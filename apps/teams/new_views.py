#Get the main project for a team Amara, universalsubtitles.org
#
# Copyright (C) 2013 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

"""new_views -- New team views

This module holds view functions for new-style teams.  Eventually it should
replace the old views.py module.
"""

from __future__ import absolute_import
import functools
import json
import logging
import pickle
from collections import namedtuple, OrderedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.paginator import Paginator
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.http import (Http404, HttpResponse, HttpResponseRedirect,
                         HttpResponseBadRequest, HttpResponseForbidden)
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.translation import ugettext as _, ungettext

from . import views as old_views
from . import forms
from . import permissions
from . import signals
from . import tasks
from .behaviors import get_main_project
from .bulk_actions import add_videos_from_csv
from .exceptions import ApplicationInvalidException
from .models import (Invite, Setting, Team, Project, TeamVideo,
                     TeamLanguagePreference, TeamMember, Application)
from .statistics import compute_statistics
from activity.models import ActivityRecord
from auth.models import CustomUser as User
from messages import tasks as messages_tasks
from subtitles.models import SubtitleLanguage
from teams.workflows import TeamWorkflow
from ui.forms import ManagementFormList
from utils.ajax import AJAXResponseRenderer
from utils.breadcrumbs import BreadCrumb
from utils.decorators import staff_member_required
from utils.pagination import AmaraPaginator, AmaraPaginatorFuture
from utils.forms import autocomplete_user_view, FormRouter
from utils.text import fmt
from utils.translation import get_language_label
from videos.models import Video

logger = logging.getLogger('teams.views')

ACTIONS_PER_PAGE = 20
VIDEOS_PER_PAGE = 12
VIDEOS_PER_PAGE_MANAGEMENT = 20
MEMBERS_PER_PAGE = 10

def team_view(view_func):
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        if not request.user.is_authenticated():
            return redirect_to_login(request.path)
        if isinstance(slug, Team):
            # we've already fetched the team in with_old_view
            team = slug
        else:
            try:
                team = Team.objects.get(slug=slug)
            except Team.DoesNotExist:
                raise Http404
        if not team.user_is_member(request.user):
            raise Http404
        return view_func(request, team, *args, **kwargs)
    return wrapper

def with_old_view(old_view_func):
    def wrap(view_func):
        @functools.wraps(view_func)
        def wrapper(request, slug, *args, **kwargs):
            try:
                team = Team.objects.get(slug=slug)
            except Team.DoesNotExist:
                raise Http404
            if team.is_old_style():
                return old_view_func(request, team, *args, **kwargs)
            return view_func(request, team, *args, **kwargs)
        return wrapper
    return wrap

def admin_only_view(view_func):
    @functools.wraps(view_func)
    @team_view
    def wrapper(request, team, *args, **kwargs):
        member = team.get_member(request.user)
        if not member.is_admin():
            messages.error(request,
                           _("You are not authorized to see this page"))
            return redirect(team)
        return view_func(request, team, member, *args, **kwargs)
    return wrapper

def public_team_view(view_func):
    def wrapper(request, slug, *args, **kwargs):
        try:
            team = Team.objects.get(slug=slug)
        except Team.DoesNotExist:
            raise Http404
        return view_func(request, team, *args, **kwargs)
    return wrapper

def team_settings_view(view_func):
    """Decorator for the team settings pages."""
    @functools.wraps(view_func)
    def wrapper(request, slug, *args, **kwargs):
        team = get_object_or_404(Team, slug=slug)
        if not permissions.can_view_settings_tab(team, request.user):
            messages.error(request,
                           _(u'You do not have permission to edit this team.'))
            return HttpResponseRedirect(team.get_absolute_url())
        return view_func(request, team, *args, **kwargs)
    return login_required(wrapper)

@with_old_view(old_views.detail)
@team_view
def videos(request, team):
    filters_form = forms.VideoFiltersForm(team, request.GET)
    videos = filters_form.get_queryset().select_related('teamvideo',
                                                        'teamvideo__video')

    paginator = AmaraPaginatorFuture(videos, VIDEOS_PER_PAGE)
    page = paginator.get_page(request)
    add_completed_subtitles_count(list(page))
    context = {
        'team': team,
        'page': page,
        'paginator': paginator,
        'filters_form': filters_form,
        'team_nav': 'videos',
        'current_tab': 'videos',
        'extra_tabs': team.new_workflow.team_video_page_extra_tabs(request),
    }
    if request.is_ajax():
        response_renderer = AJAXResponseRenderer(request)
        response_renderer.replace(
            '#video-list', 'future/teams/videos/list.html', context
        )
        return response_renderer.render()

    return render(request, 'future/teams/videos/videos.html', context)

def add_completed_subtitles_count(videos):
    counts = SubtitleLanguage.count_completed_subtitles(videos)
    for v in videos:
        count = counts[v.id][1]
        msg = ungettext((u'%(count)s completed subtitle'),
                        (u'%(count)s completed subtitles'),
                        count)
        v.completed_subtitles = fmt(msg, count=count)

@with_old_view(old_views.detail_members)
@team_view
def members(request, team):
    member = team.get_member(request.user)

    filters_form = forms.MemberFiltersForm(request.GET)

    if request.method == 'POST':
        edit_form = forms.EditMembershipForm(member, request.POST)
        if edit_form.is_valid():
            edit_form.save()
            return HttpResponseRedirect(request.path)
        else:
            logger.warning("Error updating team memership: %s (%s)",
                           edit_form.errors.as_text(),
                           request.POST)
            messages.warning(request, _(u'Error updating membership'))
    else:
        edit_form = forms.EditMembershipForm(member)

    members = filters_form.update_qs(
        team.members.select_related('user')
        .prefetch_related('user__userlanguage_set',
                          'projects_managed',
                          'languages_managed'))

    paginator = AmaraPaginator(members, MEMBERS_PER_PAGE)
    page = paginator.get_page(request)

    return render(request, 'new-teams/members.html', {
        'team': team,
        'page': page,
        'filters_form': filters_form,
        'edit_form': edit_form,
        'show_invite_link': permissions.can_invite(team, request.user),
        'show_add_link': permissions.can_add_members(team, request.user),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Members')),
        ],
    })

@team_view
def project(request, team, project_slug):
    project = get_object_or_404(team.project_set, slug=project_slug)
    if permissions.can_change_project_managers(team, request.user):
        form = request.POST.get('form')
        if request.method == 'POST' and form == 'add':
            add_manager_form = forms.AddProjectManagerForm(
                team, project, data=request.POST)
            if add_manager_form.is_valid():
                add_manager_form.save()
                member = add_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s added as a manager'), user=member.user)
                messages.success(request, msg)
                return redirect('teams:project', team.slug, project.slug)
        else:
            add_manager_form = forms.AddProjectManagerForm(team, project)

        if request.method == 'POST' and form == 'remove':
            remove_manager_form = forms.RemoveProjectManagerForm(
                team, project, data=request.POST)
            if remove_manager_form.is_valid():
                remove_manager_form.save()
                member = remove_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s removed as a manager'),
                          user=member.user)
                messages.success(request, msg)
                return redirect('teams:project', team.slug, project.slug)
        else:
            remove_manager_form = forms.RemoveProjectManagerForm(team, project)
    else:
        add_manager_form = None
        remove_manager_form = None

    data = {
        'team': team,
        'project': project,
        'managers': project.managers.all(),
        'add_manager_form': add_manager_form,
        'remove_manager_form': remove_manager_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(project),
        ],
    }
    return team.new_workflow.render_project_page(request, team, project, data)

@team_view
def all_languages_page(request, team):
    video_language_counts = dict(team.get_video_language_counts())
    completed_language_counts = dict(team.get_completed_language_counts())

    all_languages = set(video_language_counts.keys() +
                        completed_language_counts.keys())
    languages = [
        (lc,
         get_language_label(lc),
         video_language_counts.get(lc, 0),
         completed_language_counts.get(lc, 0),
        )
        for lc in all_languages
        if lc != ''
    ]
    languages.sort(key=lambda row: (-row[2], row[1]))

    data = {
        'team': team,
        'languages': languages,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Languages')),
        ],
    }
    return team.new_workflow.render_all_languages_page(
        request, team, data,
    )

@team_view
def language_page(request, team, language_code):
    try:
        language_label = get_language_label(language_code)
    except KeyError:
        raise Http404
    if permissions.can_change_language_managers(team, request.user):
        form = request.POST.get('form')
        if request.method == 'POST' and form == 'add':
            add_manager_form = forms.AddLanguageManagerForm(
                team, language_code, data=request.POST)
            if add_manager_form.is_valid():
                add_manager_form.save()
                member = add_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s added as a manager'), user=member.user)
                messages.success(request, msg)
                return redirect('teams:language-page', team.slug,
                                language_code)
        else:
            add_manager_form = forms.AddLanguageManagerForm(team,
                                                            language_code)

        if request.method == 'POST' and form == 'remove':
            remove_manager_form = forms.RemoveLanguageManagerForm(
                team, language_code, data=request.POST)
            if remove_manager_form.is_valid():
                remove_manager_form.save()
                member = remove_manager_form.cleaned_data['member']
                msg = fmt(_(u'%(user)s removed as a manager'),
                          user=member.user)
                messages.success(request, msg)
                return redirect('teams:language-page', team.slug,
                                language_code)
        else:
            remove_manager_form = forms.RemoveLanguageManagerForm(
                team, language_code)
    else:
        add_manager_form = None
        remove_manager_form = None

    data = {
        'team': team,
        'language_code': language_code,
        'language': language_label,
        'managers': (team.members
                     .filter(languages_managed__code=language_code)),
        'add_manager_form': add_manager_form,
        'remove_manager_form': remove_manager_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Languages'), 'teams:all-languages-page', team.slug),
            BreadCrumb(language_label),
        ],
    }
    return team.new_workflow.render_language_page(
        request, team, language_code, data,
    )

@team_view
def add_members(request, team):
    summary = None
    if not permissions.can_add_members(team, request.user):
        return HttpResponseForbidden(_(u'You cannot invite people to this team.'))
    if request.POST:
        form = forms.AddMembersForm(team, request.user, request.POST)
        if form.is_valid():
            summary = form.save()

    form = forms.AddMembersForm(team, request.user)

    if team.is_old_style():
        template_name = 'teams/add_members.html'
    else:
        template_name = 'new-teams/add_members.html'

    return render(request, template_name,  {
        'team': team,
        'form': form,
        'summary': summary,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Members'), 'teams:members', team.slug),
            BreadCrumb(_('Invite')),
        ],
    })

@team_view
def invite(request, team):
    if not permissions.can_invite(team, request.user):
        return HttpResponseForbidden(_(u'You cannot invite people to this team.'))
    if request.POST:
        form = forms.InviteForm(team, request.user, request.POST)
        if form.is_valid():
            # the form will fire the notifications for invitees
            # this cannot be done on model signal, since you might be
            # sending invites twice for the same user, and that borks
            # the naive signal for only created invitations
            form.save()
            return HttpResponseRedirect(reverse('teams:members',
                                                args=[team.slug]))
    else:
        form = forms.InviteForm(team, request.user)

    if team.is_old_style():
        template_name = 'teams/invite_members.html'
    else:
        template_name = 'new-teams/invite.html'

    return render(request, template_name,  {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Members'), 'teams:members', team.slug),
            BreadCrumb(_('Invite')),
        ],
    })

@team_view
def autocomplete_invite_user(request, team):
    return autocomplete_user_view(request, team.invitable_users())

@team_view
def autocomplete_project_manager(request, team, project_slug):
    project = get_object_or_404(team.project_set, slug=project_slug)
    return autocomplete_user_view(request, project.potential_managers())

@team_view
def autocomplete_language_manager(request, team, language_code):
    return autocomplete_user_view(
        request,
        team.potential_language_managers(language_code))

def member_search(request, team, qs):
    query = request.GET.get('query')
    if query:
        members_qs = (qs.filter(user__username__icontains=query)
                      .select_related('user'))
    else:
        members_qs = TeamMember.objects.none()

    data = [
        {
            'value': member.user.username,
            'label': fmt(_('%(username)s (%(full_name)s)'),
                         username=member.user.username,
                         full_name=unicode(member.user)),
        }
        for member in members_qs
    ]

    return HttpResponse(json.dumps(data), mimetype='application/json')

@public_team_view
@login_required
def join(request, team):
    user = request.user

    if team.user_is_member(request.user):
        messages.info(request,
                      fmt(_(u'You are already a member of %(team)s.'),
                          team=team))
    elif team.is_open():
        member = TeamMember.objects.create(team=team, user=request.user,
                                           role=TeamMember.ROLE_CONTRIBUTOR)
        messages.success(request,
                         fmt(_(u'You are now a member of %(team)s.'),
                             team=team))
        messages_tasks.team_member_new.delay(member.pk)
    elif team.is_by_application():
        return application_form(request, team)
    else:
        messages.error(request,
                       fmt(_(u'You cannot join %(team)s.'), team=team))
    return redirect(team)

def application_form(request, team):
    try:
        application = team.applications.get(user=request.user)
    except Application.DoesNotExist:
        application = Application(team=team, user=request.user)
    try:
        application.check_can_submit()
    except ApplicationInvalidException, e:
        messages.error(request, e.message)
        return redirect(team)

    if request.method == 'POST':
        form = forms.ApplicationForm(application, data=request.POST)
        if form.is_valid():
            form.save()
            return redirect(team)
    else:
        form = forms.ApplicationForm(application)
    return render(request, "new-teams/application.html", {
        'team': team,
        'form': form,
    })

@public_team_view
def admin_list(request, team):
    if team.is_old_style():
        return old_views.detail_members(request, team,
                                        role=TeamMember.ROLE_ADMIN)

    # The only real reason to view this page is if you want to ask an admin to
    # invite you, so let's limit the access a bit
    if (not team.is_by_invitation() and not
        team.user_is_member(request.user)):
        return HttpResponseForbidden()
    return render(request, 'new-teams/admin-list.html', {
        'team': team,
        'admins': (team.members
                   .filter(Q(role=TeamMember.ROLE_ADMIN)|
                           Q(role=TeamMember.ROLE_OWNER))
                   .select_related('user'))
    })

@team_view
def activity(request, team):
    filters_form = forms.ActivityFiltersForm(team, request.GET)
    paginator = AmaraPaginator(filters_form.get_queryset(), ACTIONS_PER_PAGE)
    page = paginator.get_page(request)

    action_choices = ActivityRecord.type_choices()

    next_page_query = request.GET.copy()
    next_page_query['page'] = page.next_page_number()

    context = {
        'paginator': paginator,
        'page': page,
        'filters_form': filters_form,
        'filtered': filters_form.is_bound,
        'team': team,
        'tab': 'activity',
        'user': request.user,
        'next_page_query': next_page_query.urlencode(),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Activity')),
        ],
    }
    if team.is_old_style():
        template_dir = 'teams/'
    else:
        template_dir = 'new-teams/'

    if not request.is_ajax():
        return render(request, template_dir + 'activity.html', context)
    else:
        # for ajax requests we only want to return the activity list, since
        # that's all that the JS code needs.
        return render(request, template_dir + '_activity-list.html', context)

@team_view
def statistics(request, team, tab):
    """For the team activity, statistics tabs
    """
    if (tab == 'teamstats' and
        not permissions.can_view_stats_tab(team, request.user)):
        return HttpResponseForbidden("Not allowed")
    cache_key = 'stats-' + team.slug + '-' + tab
    cached_context = cache.get(cache_key)
    if cached_context:
        context = pickle.loads(cached_context)
    else:
        context = compute_statistics(team, stats_type=tab)
        cache.set(cache_key, pickle.dumps(context), 60*60*24)
    context['tab'] = tab
    context['team'] = team
    context['breadcrumbs'] = [
        BreadCrumb(team, 'teams:dashboard', team.slug),
        BreadCrumb(_('Activity')),
    ]
    if team.is_old_style():
        return render(request, 'teams/statistics.html', context)
    else:
        return render(request, 'new-teams/statistics.html', context)


def dashboard(request, slug):
    team = get_object_or_404(
        Team.objects.for_user(request.user, exclude_private=False),
        slug=slug)
    if not team.is_old_style() and not team.user_is_member(request.user):
        return welcome(request, team)
    else:
        return team.new_workflow.dashboard_view(request, team)

def welcome(request, team):
    if team.is_visible:
        videos = team.videos.order_by('-id')[:2]
    else:
        videos = None

    if Application.objects.open(team, request.user):
        messages.info(request,
                      _(u"Your application has been submitted. "
                        u"You will be notified of the team "
                        "administrator's response"))

    return render(request, 'new-teams/welcome.html', {
        'team': team,
        'join_mode': team.get_join_mode(request.user),
        'team_messages': team.get_messages([
            'pagetext_welcome_heading',
        ]),
        'videos': videos,
    })

@team_view
def manage_videos(request, team):
    filters_form = forms.ManagementVideoFiltersForm(team, request.GET,
                                                    auto_id="id_filters_%s")
    videos = filters_form.get_queryset().select_related('teamvideo',
                                                        'teamvideo__video')
    form_name = request.GET.get('form')
    if form_name:
        if form_name == 'add-video':
            return add_video_form(request, team)
        else:
            return manage_videos_form(request, team, form_name, videos)
    paginator = AmaraPaginatorFuture(videos, VIDEOS_PER_PAGE_MANAGEMENT)
    page = paginator.get_page(request)
    team.new_workflow.video_management_add_counts(list(page))
    context = {
        'team': team,
        'page': page,
        'paginator': paginator,
        'filters_form': filters_form,
        'team_nav': 'management',
        'current_tab': 'videos',
        'extra_tabs': team.new_workflow.management_page_extra_tabs(request),
        'manage_forms': [
            (form.name, form.css_class, form.label)
            for form in all_video_management_forms(team, request.user)
        ],
    }
    if request.is_ajax():
        response_renderer = AJAXResponseRenderer(request)
        response_renderer.replace(
            '#video-list', 'future/teams/management/video-list.html', context
        )
        return response_renderer.render()

    return render(request, 'future/teams/management/videos.html', context)

# Functions to handle the forms on the videos pages
def get_video_management_forms(team):
    form_list = ManagementFormList([
        forms.EditVideosForm,
        forms.MoveVideosForm,
    ])
    signals.build_video_management_forms.send(sender=team, form_list=form_list)
    form_list.extend([
        forms.DeleteVideosForm
    ])
    return form_list

def all_video_management_forms(team, user):
    return get_video_management_forms(team).all(team, user)

def lookup_video_managment_form(team, user, form_name):
    return get_video_management_forms(team).lookup(form_name, team, user)

def manage_videos_form(request, team, form_name, videos):
    """Render a form from the action bar on the video management page.
    """
    try:
        selection = request.GET['selection'].split('-')
    except StandardError:
        return HttpResponseBadRequest()
    FormClass = lookup_video_managment_form(team, request.user, form_name)
    if FormClass is None:
        raise Http404()

    all_selected = len(selection) >= VIDEOS_PER_PAGE_MANAGEMENT
    if request.method == 'POST':
        form = FormClass(team, request.user, videos, selection, all_selected,
                         data=request.POST, files=request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, form.message())
            response = HttpResponse("SUCCESS", content_type="text/plain")
            response['X-Form-Success'] = '1'
            return response
    else:
        form = FormClass(team, request.user, videos, selection, all_selected)

    first_video = Video.objects.get(id=selection[0])
    template_name = 'future/teams/management/video-forms/{}.html'.format(
        form_name)
    return render(request, template_name, {
        'team': team,
        'form': form,
        'first_video': first_video,
        'selection_count': len(selection),
        'single_selection': len(selection) == 1,
        'all_selected': all_selected,
    })

def add_video_form(request, team):
    if request.method == 'POST':
        form = forms.AddTeamVideoForm(team, request.user, data=request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, form.success_message())
            response = HttpResponse("SUCCESS", content_type="text/plain")
            response['X-Form-Success'] = '1'
            return response
    else:
        form = forms.AddTeamVideoForm(team, request.user)
    form.use_future_ui()

    if form.is_bound and form.is_valid():
        form.save()
        messages.success(request, form.message())
        response = HttpResponse("SUCCESS", content_type="text/plain")
        response['X-Form-Success'] = '1'
        return response
    template_name = 'future/teams/management/video-forms/add-video.html'
    return render(request, template_name, {
        'team': team,
        'form': form,
    })

@team_settings_view
def settings_basic(request, team):
    if team.is_old_style():
        return old_views.settings_basic(request, team)

    if permissions.can_rename_team(team, request.user):
        FormClass = forms.RenameableSettingsForm
    else:
        FormClass = forms.SettingsForm

    if request.POST:
        form = FormClass(request.POST, request.FILES, instance=team)

        is_visible = team.is_visible

        if form.is_valid():
            try:
                form.save()
            except:
                logger.exception("Error on changing team settings")
                raise

            if is_visible != form.instance.is_visible:
                tasks.update_video_public_field.delay(team.id)
                tasks.invalidate_video_visibility_caches.delay(team)

            messages.success(request, _(u'Settings saved.'))
            return HttpResponseRedirect(request.path)
    else:
        form = FormClass(instance=team)

    return render(request, "new-teams/settings.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings')),
        ],
    })

@team_settings_view
def settings_messages(request, team):
    if team.is_old_style():
        return old_views.settings_messages(request, team)

    initial = team.settings.all_messages()
    if request.POST:
        form = forms.GuidelinesMessagesForm(request.POST, initial=initial)

        if form.is_valid():
            for key, val in form.cleaned_data.items():
                setting, c = Setting.objects.get_or_create(team=team, key=Setting.KEY_IDS[key])
                setting.data = val
                setting.save()

            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = forms.GuidelinesMessagesForm(initial=initial)

    return render(request, "new-teams/settings-messages.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Messages')),
        ],
    })

@team_settings_view
def settings_lang_messages(request, team):
    if team.is_old_style():
        return old_views.settings_lang_messages(request, team)

    initial = team.settings.all_messages()
    languages = [{"code": l.language_code, "data": l.data} for l in team.settings.localized_messages()]
    if request.POST:
        form = forms.GuidelinesLangMessagesForm(request.POST, languages=languages)
        if form.is_valid():
            new_language = None
            new_message = None
            for key, val in form.cleaned_data.items():
                if key == "messages_joins_localized":
                    new_message = val
                elif key == "messages_joins_language":
                    new_language = val
                else:
                    l = key.split("messages_joins_localized_")
                    if len(l) == 2:
                        code = l[1]
                        try:
                            setting = Setting.objects.get(team=team, key=Setting.KEY_IDS["messages_joins_localized"], language_code=code)
                            if val == "":
                                setting.delete()
                            else:
                                setting.data = val
                                setting.save()
                        except:
                            messages.error(request, _(u'No message for that language.'))
                            return HttpResponseRedirect(request.path)
            if new_message and new_language:
                setting, c = Setting.objects.get_or_create(team=team,
                                  key=Setting.KEY_IDS["messages_joins_localized"],
                                  language_code=new_language)
                if c:
                    setting.data = new_message
                    setting.save()
                else:
                    messages.error(request, _(u'There is already a message for that language.'))
                    return HttpResponseRedirect(request.path)
            elif new_message or new_language:
                messages.error(request, _(u'Please set the language and the message.'))
                return HttpResponseRedirect(request.path)
            messages.success(request, _(u'Guidelines and messages updated.'))
            return HttpResponseRedirect(request.path)
    else:
        form = forms.GuidelinesLangMessagesForm(languages=languages)

    return render(request, "new-teams/settings-lang-messages.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Language-specific Messages')),
        ],
    })

@team_settings_view
def settings_feeds(request, team):
    if team.is_old_style():
        return old_views.video_feeds(request, team)

    action = request.POST.get('action')
    if request.method == 'POST' and action == 'import':
        feed = get_object_or_404(team.videofeed_set, id=request.POST['feed'])
        feed.update()
        messages.success(request, _(u'Importing videos now'))
        return HttpResponseRedirect(request.build_absolute_uri())
    if request.method == 'POST' and action == 'delete':
        feed = get_object_or_404(team.videofeed_set, id=request.POST['feed'])
        feed.delete()
        messages.success(request, _(u'Feed deleted'))
        return HttpResponseRedirect(request.build_absolute_uri())

    if request.method == 'POST' and action == 'add':
        add_form = forms.AddTeamVideosFromFeedForm(team, request.user,
                                                   data=request.POST)
        if add_form.is_valid():
            add_form.save()
            messages.success(request, _(u'Video Feed Added'))
            return HttpResponseRedirect(request.build_absolute_uri())
    else:
        add_form = forms.AddTeamVideosFromFeedForm(team, request.user)

    return render(request, "new-teams/settings-feeds.html", {
        'team': team,
        'add_form': add_form,
        'feeds': team.videofeed_set.all(),
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Video Feeds')),
        ],
    })

@team_settings_view
def settings_projects(request, team):
    if team.is_old_style():
        return old_views.settings_projects(request, team)

    projects = Project.objects.for_team(team)

    form = request.POST.get('form')

    if request.method == 'POST' and form == 'add':
        add_form = forms.ProjectForm(team, data=request.POST)

        if add_form.is_valid():
            add_form.save()
            messages.success(request, _('Project added.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        add_form = forms.ProjectForm(team)

    if request.method == 'POST' and form == 'edit':
        edit_form = forms.EditProjectForm(team, data=request.POST)

        if edit_form.is_valid():
            edit_form.save()
            messages.success(request, _('Project updated.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        edit_form = forms.EditProjectForm(team)

    if request.method == 'POST' and form == 'delete':
        try:
            project = projects.get(id=request.POST['project'])
        except Project.DoesNotExist:
            pass
        else:
            project.delete()
            messages.success(request, _('Project deleted.'))
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )

    return render(request, "new-teams/settings-projects.html", {
        'team': team,
        'projects': projects,
        'add_form': add_form,
        'edit_form': edit_form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Projects')),
        ],
    })

@team_settings_view
def edit_project(request, team, project_slug):
    if team.is_old_style():
        return old_views.edit_project(request, team, project_slug)

    project = get_object_or_404(Project, slug=project_slug)
    if 'delete' in request.POST:
        project.delete()
        return HttpResponseRedirect(
            reverse('teams:settings_projects', args=(team.slug,))
        )
    elif request.POST:
        form = forms.ProjectForm(team, instance=project, data=request.POST)

        if form.is_valid():
            form.save()
            return HttpResponseRedirect(
                reverse('teams:settings_projects', args=(team.slug,))
            )
    else:
        form = forms.ProjectForm(team, instance=project)

    return render(request, "new-teams/settings-projects-edit.html", {
        'team': team,
        'form': form,
        'breadcrumbs': [
            BreadCrumb(team, 'teams:dashboard', team.slug),
            BreadCrumb(_('Settings'), 'teams:settings_basic', team.slug),
            BreadCrumb(_('Projects'), 'teams:settings_projects', team.slug),
            BreadCrumb(project.name),
        ],
    })

@team_settings_view
def settings_workflows(request, team):
    return team.new_workflow.workflow_settings_view(request, team)

@staff_member_required
@team_view
def video_durations(request, team):
    projects = team.projects_with_video_stats()
    totals = (
        sum(p.video_count for p in projects),
        sum(p.videos_without_duration for p in projects),
        sum(p.total_duration for p in projects),
    )
    return render(request, "new-teams/video-durations.html", {
        'team': team,
        'projects': projects,
        'totals': totals,
    })
