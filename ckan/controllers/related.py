

import ckan.model as model
import ckan.logic as logic
import ckan.lib.base as base
import ckan.lib.helpers as h

c = base.c

class RelatedController(base.BaseController):

    def list(self, id):

        context = {'model': model, 'session': model.Session,
                   'user': c.user or c.author, 'extras_as_string': True,
                   'for_view': True}
        data_dict = {'id': id}

        try:
            logic.check_access('package_show', context, data_dict)
        except logic.NotFound:
            base.abort(404, base._('Dataset not found'))
        except logic.NotAuthorized:
            base.abort(401, base._('Not authorized to see this page'))

        try:
            c.pkg_dict = logic.get_action('package_show')(context, data_dict)
            c.pkg = context['package']
            c.resources_json = h.json.dumps(c.pkg_dict.get('resources',[]))
        except logic.NotFound:
            base.abort(404, base._('Dataset not found'))
        except logic.NotAuthorized:
            base.abort(401, base._('Unauthorized to read package %s') % id)

        c.related_count = len(c.pkg.related)

        c.num_followers = logic.get_action('dataset_follower_count')(context,
                {'id': c.pkg_dict['id']})
        # If the user is logged in set the am_following variable.
        if c.user:
            c.pkg_dict['am_following'] = logic.get_action('am_following_dataset')(
                context, {'id': c.pkg.id})

        return base.render( "package/related_list.html")

