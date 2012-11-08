import logging
from pylons import config, c

from ckan import model
from ckan.model import DomainObjectOperation
from ckan.plugins import SingletonPlugin, implements, IDomainObjectModification
from ckan.logic import get_action

from common import (SearchIndexError, SearchError, SearchQueryError,
                    make_connection, is_available, SolrSettings)
from index import PackageSearchIndex, NoopSearchIndex
from query import TagSearchQuery, ResourceSearchQuery, PackageSearchQuery, QueryOptions, convert_legacy_parameters_to_solr

log = logging.getLogger(__name__)

import sys
import cgitb
import warnings
def text_traceback():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = 'the original traceback:'.join(
            cgitb.text(sys.exc_info()).split('the original traceback:')[1:]
        ).strip()
    return res

SIMPLE_SEARCH = config.get('ckan.simple_search', False)

SUPPORTED_SCHEMA_VERSIONS = ['1.4']

DEFAULT_OPTIONS = {
    'limit': 20,
    'offset': 0,
    # about presenting the results
    'order_by': 'rank',
    'return_objects': False,
    'ref_entity_with_attr': 'name',
    'all_fields': False,
    'search_tags': True,
    'callback': None, # simply passed through
}

_INDICES = {
    'package': PackageSearchIndex
}

_QUERIES = {
    'tag': TagSearchQuery,
    'resource': ResourceSearchQuery,
    'package': PackageSearchQuery
}

SOLR_SCHEMA_FILE_OFFSET = '/admin/file/?file=schema.xml'

if SIMPLE_SEARCH:
    import sql as sql
    _INDICES['package'] = NoopSearchIndex
    _QUERIES['package'] = sql.PackageSearchQuery

def _normalize_type(_type):
    if isinstance(_type, model.DomainObject):
        _type = _type.__class__
    if isinstance(_type, type):
        _type = _type.__name__
    return _type.strip().lower()

def index_for(_type):
    """ Get a SearchIndex instance sub-class suitable for the specified type. """
    try:
        _type_n = _normalize_type(_type)
        return _INDICES[_type_n]()
    except KeyError, ke:
        log.warn("Unknown search type: %s" % _type)
        return NoopSearchIndex()

def query_for( _type):
    """ Get a SearchQuery instance sub-class suitable for the specified type. """
    try:
        _type_n = _normalize_type(_type)
        return _QUERIES[_type_n]()
    except KeyError, ke:
        raise SearchError("Unknown search type: %s" % _type)

def dispatch_by_operation(entity_type, entity, operation):
    """Call the appropriate index method for a given notification."""
    try:
        index = index_for(entity_type)
        if operation == DomainObjectOperation.new:
            index.insert_dict(entity)
        elif operation == DomainObjectOperation.changed:
            index.update_dict(entity)
        elif operation == DomainObjectOperation.deleted:
            index.remove_dict(entity)
        else:
            log.warn("Unknown operation: %s" % operation)
    except Exception, ex:
        log.exception(ex)
        # we really need to know about any exceptions, so reraise
        # (see #1172)
        raise


class SynchronousSearchPlugin(SingletonPlugin):
    """Update the search index automatically."""
    implements(IDomainObjectModification, inherit=True)

    def notify(self, entity, operation):
        if not isinstance(entity, model.Package):
            return
        if operation != DomainObjectOperation.deleted:
            dispatch_by_operation(
                entity.__class__.__name__,
                get_action('package_show')(
                    {'model': model, 'ignore_auth': True, 'validate': False},
                    {'id': entity.id}),
                operation
            )
        elif operation == DomainObjectOperation.deleted:
            dispatch_by_operation(entity.__class__.__name__,
                                  {'id': entity.id}, operation)
        else:
            log.warn("Discarded Sync. indexing for: %s" % entity)

def rebuild(package_id=None,only_missing=False,force=False,refresh=False):
    '''
        Rebuilds the search index.

        If a dataset id is provided, only this dataset will be reindexed.
        When reindexing all datasets, if only_missing is True, only the
        datasets not already indexed will be processed. If force equals
        True, if an execption is found, the exception will be logged, but
        the process will carry on.
    '''
    from ckan import model
    log.debug("Rebuilding search index...")

    package_index = index_for(model.Package)

    if package_id:
        pkg_dict = get_action('package_show')(
                    {'model': model, 'ignore_auth': True, 'validate': False},
                    {'id': package_id})
        package_index.remove_dict(pkg_dict)
        package_index.insert_dict(pkg_dict)
    else:
        package_ids = [r[0] for r in model.Session.query(model.Package.id).filter(model.Package.state == 'active').order_by(model.Package.name).all()]
        if only_missing:
            log.debug('Indexing only missing packages...')
            package_query = query_for(model.Package)
            indexed_pkg_ids = set(package_query.get_all_entity_ids(max_results=len(package_ids)))
            package_ids = set(package_ids) - indexed_pkg_ids     # Packages not indexed

            if len(package_ids) == 0:
                log.debug('All datasets are already indexed')
                return
        elif refresh:
            log.debug('Refreshing the index...')
            # The index is not previously cleared,
            # but remove from the index any stray ones
            # TODO Clear stray ones
            pkgs_q = model.Session.query(model.Package).filter_by(state=model.State.ACTIVE)
            pkgs = set([pkg.id for pkg in pkgs_q])
            log.debug('%i datasets in the database', len(pkgs))
            package_query = query_for(model.Package)
            indexed_pkgs = set(package_query.get_all_entity_ids(max_results=len(pkgs)*2))
            log.debug('%i datasets in the search index', len(indexed_pkgs))
            pkgs_indexed_but_object_missing = indexed_pkgs - pkgs
            log.debug('Found %i datasets in the index which have no equivalent in the database',
                      len(pkgs_indexed_but_object_missing))
            if pkgs_indexed_but_object_missing:
                pkg_names = [package_query.get_index(pkg_id)['name'] \
                             for pkg_id in pkgs_indexed_but_object_missing[:5]]
                print 'Datasets just in the index (limit=5): %r' % pkg_names
        else:
            log.debug('Rebuilding the whole index from scratch...')
            package_index.clear()

        for pkg_id in package_ids:
            try:
                package_index.insert_dict(
                    get_action('package_show')(
                        {'model': model, 'ignore_auth': True, 'validate': False},
                        {'id': pkg_id}
                    )
                )
            except Exception,e:
                log.error('Error while indexing dataset %s: %s' % (pkg_id,str(e)))
                if force:
                    log.error(text_traceback())
                    continue
                else:
                    raise

    model.Session.commit()
    log.debug('Finished rebuilding search index.')

def check():
    from ckan import model
    package_query = query_for(model.Package)

    log.debug("Checking packages search index...")
    pkgs_q = model.Session.query(model.Package).filter_by(state=model.State.ACTIVE)
    pkgs = set([pkg.id for pkg in pkgs_q])
    indexed_pkgs = set(package_query.get_all_entity_ids(max_results=len(pkgs)))
    pkgs_not_indexed = pkgs - indexed_pkgs
    print 'Packages not indexed = %i out of %i' % (len(pkgs_not_indexed), len(pkgs))
    for pkg_id in pkgs_not_indexed:
        pkg = model.Session.query(model.Package).get(pkg_id)
        print pkg.revision.timestamp.strftime('%Y-%m-%d'), pkg.name

def show(package_reference):
    from ckan import model
    package_query = query_for(model.Package)

    return package_query.get_index(package_reference)

def clear(package_reference=None):
    from ckan import model
    package_index = index_for(model.Package)
    if package_reference:
        log.debug("Clearing search index for dataset %s..." % package_reference)
        package_index.delete_package({'id':package_reference})
    else:
        log.debug("Clearing search index...")
        package_index.clear()


def check_solr_schema_version(schema_file=None):
    '''
        Checks if the schema version of the SOLR server is compatible
        with this CKAN version.

        The schema will be retrieved from the SOLR server, using the
        offset defined in SOLR_SCHEMA_FILE_OFFSET
        ('/admin/file/?file=schema.xml'). The schema_file parameter
        allows to override this pointing to different schema file, but
        it should only be used for testing purposes.

        If the CKAN instance is configured to not use SOLR or the SOLR
        server is not available, the function will return False, as the
        version check does not apply. If the SOLR server is available,
        a SearchError exception will be thrown if the version could not
        be extracted or it is not included in the supported versions list.

        :schema_file: Absolute path to an alternative schema file. Should
                      be only used for testing purposes (Default is None)
    '''

    import urllib2

    if SIMPLE_SEARCH:
        # Not using the SOLR search backend
        return False

    if not is_available():
        # Something is wrong with the SOLR server
        log.warn('Problems were found while connecting to the SOLR server')
        return False

    # Try to get the schema XML file to extract the version
    if not schema_file:
        solr_url, solr_user, solr_password = SolrSettings.get()

        http_auth = None
        if solr_user is not None and solr_password is not None:
            http_auth = solr_user + ':' + solr_password
            http_auth = 'Basic ' + http_auth.encode('base64').strip()

        url = solr_url.strip('/') + SOLR_SCHEMA_FILE_OFFSET

        req = urllib2.Request(url = url)
        if http_auth:
            req.add_header('Authorization',http_auth)

        res = urllib2.urlopen(req)
    else:
        url = 'file://%s' % schema_file
        res = urllib2.urlopen(url)

    from lxml import etree
    tree = etree.fromstring(res.read())

    version = tree.xpath('//schema/@version')
    if not len(version):
        raise SearchError('Could not extract version info from the SOLR schema, using file: \n%s' % url)
    version = version[0]

    if not version in SUPPORTED_SCHEMA_VERSIONS:
        raise SearchError('SOLR schema version not supported: %s. Supported versions are [%s]'
                % (version,', '.join(SUPPORTED_SCHEMA_VERSIONS)))
    return True
