import csv
import json
from datetime import datetime

from django.db import transaction
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.conf import settings

from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.auth.models import User

import fields

from importer.tasks import run_import_event_validation,\
    commit_import_event

from treemap.models import Species, Neighborhood, Plot,\
    Tree, ExclusionMask

from importer.models import TreeImportEvent, TreeImportRow,\
    SpeciesImportEvent, SpeciesImportRow, \
    GenericImportEvent, GenericImportRow

from importer import errors

def lowerkeys(h):
    h2 = {}
    for (k,v) in h.iteritems():
        k = k.lower().strip()
        if k != 'ignore':
            h2[k] = v.strip()

    return h2

def start(request):
    return render_to_response('importer/index.html', RequestContext(request,{}))

def find_similar_species(request):
    target = request.REQUEST['target']

    species = Species.objects\
                     .extra(
                         select={
                             'l': "levenshtein(genus || ' ' || species || ' ' || cultivar_name || ' ' || other_part_of_name, %s)"
                         },
                         select_params=(target,))\
                     .order_by('l')[0:2] # Take top 2

    output = [{fields.trees.GENUS: s.genus,
               fields.trees.SPECIES: s.species,
               fields.trees.CULTIVAR: s.cultivar_name,
               fields.trees.OTHER_PART_OF_NAME: s.other_part_of_name,
               'pk': s.pk} for s in species]

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')

def counts(request):
    active_trees = TreeImportEvent\
        .objects\
        .order_by('id')\
        .exclude(status=GenericImportEvent.FINISHED_CREATING)

    active_species = SpeciesImportEvent\
        .objects\
        .order_by('id')\
        .exclude(status=GenericImportEvent.FINISHED_CREATING)

    output = {}
    output['trees'] = {t.pk: t.row_type_counts() for t in active_trees}
    output['species'] = {s.pk: s.row_type_counts() for s in active_species }

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')


def create(request):
    if request.REQUEST['type'] == 'tree':
        typ = 'tree'
        processors = {
            'rowconstructor': TreeImportRow,
            'fileconstructor': TreeImportEvent
        }
    elif request.REQUEST['type'] == 'species':
        typ = 'species'
        processors = {
            'rowconstructor': SpeciesImportRow,
            'fileconstructor': SpeciesImportEvent
        }

    pk = process_csv(request,**processors)

    return HttpResponseRedirect(reverse('importer.views.list_imports'))

def list_imports(request):
    trees = TreeImportEvent.objects\
                           .order_by('id')

    active_trees = trees.exclude(
        status=GenericImportEvent.FINISHED_CREATING)

    finished_trees = trees.filter(
        status=GenericImportEvent.FINISHED_CREATING)

    species = SpeciesImportEvent.objects\
                                .order_by('id')

    active_species = species.exclude(
        status=GenericImportEvent.FINISHED_CREATING)

    finished_species = species.filter(
        status=GenericImportEvent.FINISHED_CREATING)

    return render_to_response(
        'importer/list.html',
        RequestContext(
            request,
            {'trees_active': active_trees,
             'trees_finished': finished_trees,
             'species_active': active_species,
             'species_finished': finished_species }))

def show_species_import_status(request, import_event_id):
    return show_import_status(request, import_event_id, SpeciesImportEvent)

def show_tree_import_status(request, import_event_id):
    return show_import_status(request, import_event_id, TreeImportEvent)

def show_import_status(request, import_event_id, Model):
    return render_to_response(
        'importer/status.html',
        RequestContext(
            request,
            {'event': Model.objects.get(pk=import_event_id)}))

def update(request, import_type, import_event_id):
    if import_type == 'tree':
        Model = TreeImportEvent
    else:
        Model = SpeciesImportEvent

    rowdata = json.loads(request.REQUEST['row'])
    idx = rowdata['id']

    row = Model.objects.get(pk=import_event_id).rows().get(idx=idx)
    basedata = row.datadict

    for k,v in rowdata.iteritems():
        if k in basedata:
            basedata[k] = v

    row.datadict = basedata
    row.save()
    row.validate_row()

    return HttpResponse()


# TODO: Remove this method
def update_row(request, import_event_row_id):
    update_keys = { key.split('update__')[1]
                    for key
                    in request.REQUEST.keys()
                    if key.startswith('update__') }

    row = TreeImportRow.objects.get(pk=import_event_row_id)

    basedata = row.datadict

    for key in update_keys:
        basedata[key] = request.REQUEST['update__%s' % key]

    row.datadict = basedata
    row.save()
    row.validate_row()

    return HttpResponseRedirect(reverse('importer.views.show_import_status',
                                        args=(row.import_event.pk,)))

def results(request, import_event_id, import_type, subtype):
    """ Return a json array for each row of a given subtype
    where subtype is a valid status for a TreeImportRow
    """
    if import_type == 'tree':
        status_map = {
            'success': TreeImportRow.SUCCESS,
            'error': TreeImportRow.ERROR,
            'waiting': TreeImportRow.WAITING,
            'watch': TreeImportRow.WATCH,
            'verified': TreeImportRow.VERIFIED
        }

        Model = TreeImportEvent
    else:
        status_map = {
            'success': SpeciesImportRow.SUCCESS,
            'error': SpeciesImportRow.ERROR,
            'verified': SpeciesImportRow.VERIFIED,
        }
        Model = SpeciesImportEvent

    page_size = 10
    page = int(request.REQUEST.get('page', 0))
    page_start = page_size * page
    page_end = page_size * (page + 1)

    ie = Model.objects.get(pk=import_event_id)

    header = None
    output = {}

    if subtype == 'mergereq':
        query = ie.rows()\
                  .filter(merged=False)\
                  .exclude(status=SpeciesImportRow.ERROR)\
                  .order_by('idx')
    else:
        query = ie.rows()\
                  .filter(status=status_map[subtype])\
                  .order_by('idx')

    if import_type == 'species' and subtype == 'verified':
        query = query.filter(merged=True)

    count = query.count()
    total_pages = int(float(count) / page_size + 1)

    output['total_pages'] = total_pages
    output['count'] = count
    output['rows'] = []

    header_keys = None
    for row in query[page_start:page_end]:
        if header is None:
            header_keys = row.datadict.keys()

        data = {
            'row': row.idx,
            'errors': row.errors_as_array(),
            'data': [row.datadict[k] for k in header_keys]
        }


        # Generate diffs for merge requests
        if subtype == 'mergereq':
            # If errors.TOO_MANY_SPECIES we need to mine species
            # otherwise we can just do simple diff
            ecodes = dict([(e['code'],e['data']) for e in row.errors_as_array()])
            if errors.TOO_MANY_SPECIES[0] in ecodes:
                data['diffs'] = ecodes[errors.TOO_MANY_SPECIES[0]]
            elif errors.MERGE_REQ[0] in ecodes:
                data['diffs'] = [ecodes[errors.MERGE_REQ[0]]]

        if hasattr(row,'plot') and row.plot:
            data['plot_id'] = row.plot.pk

        if hasattr(row,'species') and row.species:
            data['species_id'] = row.species.pk

        output['rows'].append(data)

    output['fields'] = header_keys or \
                       ie.rows()[0].datadict.keys()

    return HttpResponse(
        json.dumps(output),
        content_type = 'application/json')

def process_status(request, import_id, TheImportEvent):
    ie = TheImportEvent.objects.get(pk=import_id)

    resp = None
    if ie.errors:
        resp = {'status': 'file_error',
                'errors': json.loads(ie.errors)}
    else:
        errors = []
        for row in ie.rows():
            if row.errors:
                errors.append((row.idx, json.loads(row.errors)))

        if len(errors) > 0:
            resp = {'status': 'row_error',
                    'errors': dict(errors)}

    if resp is None:
        resp = {'status': 'success',
                'rows': ie.rows().count()}

    return HttpResponse(
        json.dumps(resp),
        content_type = 'application/json')

def solve(request, import_event_id, import_row_idx):
    ie = SpeciesImportEvent.objects.get(pk=import_event_id)
    row = ie.rows().get(idx=import_row_idx)

    data = dict(json.loads(request.REQUEST['data']))
    tgtspecies = request.REQUEST['species'];

    # Strip off merge errors
    merge_errors = { errors.TOO_MANY_SPECIES[0],
                     errors.MERGE_REQ[0] }


    ierrors = [e for e in row.errors_as_array()
               if e['code'] not in merge_errors];

    #TODO: Json handling is terrible.
    row.errors = json.dumps(ierrors)
    row.datadict = data

    if tgtspecies != 'new':
        row.species = Species.objects.get(pk=tgtspecies)

    row.merged = True
    row.save()

    rslt = row.validate_row()

    return HttpResponse(
        json.dumps({'status': 'ok',
                    'validates': rslt}),
        content_type = 'application/json')

@transaction.commit_manually
def commit(request, import_event_id, import_type=None):
    #TODO:!!! NEED TO ADD TREES TO WATCH LIST
    #TODO:!!! Trees in the same import event should not cause
    #         proximity issues
    #TODO:!!! NEED TO INDICATE TREES TO BE ADDED TO WATCH LIST
    #TODO:!!! NEED TO CLEAR TILE CACHE
    #TODO:!!! If 'Plot' already exists on row *update* when changed
    if import_type == 'species':
        model = SpeciesImportEvent
    elif import_type == 'tree':
        model = TreeImportEvent
    else:
        raise Exception('invalid import type')

    ie = model.objects.get(pk=import_event_id)
    ie.status = GenericImportEvent.CREATING

    ie.save()
    ie.rows().update(status=GenericImportRow.WAITING)

    transaction.commit()

    commit_import_event.delay(ie)
    #TODO: Update tree counts for species

    return HttpResponse(
        json.dumps({'status': 'done'}),
        content_type = 'application/json')

@transaction.commit_manually
def process_csv(request, rowconstructor, fileconstructor):
    files = request.FILES
    filename = files.keys()[0]
    fileobj = files[filename]

    owner = User.objects.all()[0]
    ie = fileconstructor(file_name=filename,
                         owner=owner)
    ie.save()

    rows = create_rows_for_event(ie, fileobj,
                                 constructor=rowconstructor)
    transaction.commit()

    if rows:
        run_import_event_validation.delay(ie)

    return ie.pk

def process_commit(request, import_id):
    ie = TreeImportEvent.objects.get(pk=import_id)

    rslt = commit_import_event(ie)

    # TODO: What to return here?
    return HttpResponse(
        json.dumps({'status': 'success'}),
        content_type = 'application/json')


def create_rows_for_event(importevent, csvfile, constructor):
    rows = []
    reader = csv.DictReader(csvfile)

    idx = 0
    for row in reader:
        rows.append(
            constructor.objects.create(
                data=json.dumps(lowerkeys(row)),
                import_event=importevent, idx=idx))

        # First row
        if idx == 0:
            # Break out early if there was an error
            # with the basic file structure
            importevent.validate_main_file()
            if importevent.has_errors():
                return False

        idx += 1

    return rows
