import csv
from datetime import datetime, timedelta

from django import forms
from django.conf.urls.defaults import patterns, url
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin import SimpleListFilter
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseRedirect

import autocomplete_light
from celery.task.sets import TaskSet
from functools import update_wrapper
from sorl.thumbnail.admin import AdminImageMixin

import mozillians.users.tasks
from mozillians.groups.models import GroupMembership, Skill
from mozillians.users.cron import index_all_profiles
from mozillians.users.models import (COUNTRIES, PUBLIC, Language,
                                     ExternalAccount,
                                     UserProfile, UsernameBlacklist)


Q_PUBLIC_PROFILES = Q()
for field in UserProfile.privacy_fields():
    key = 'privacy_%s' % field
    Q_PUBLIC_PROFILES |= Q(**{key: PUBLIC})


def export_as_csv_action(description=None, fields=None, exclude=None,
                         header=True):
    """
    This function returns an export csv action
    'fields' and 'exclude' work like in django ModelForm
    'header' is whether or not to output the column names as the first row

    Based on snippet http://djangosnippets.org/snippets/2020/
    """

    def export_as_csv(modeladmin, request, queryset):
        """
        Generic csv export admin action.
        based on http://djangosnippets.org/snippets/1697/
        """
        opts = modeladmin.model._meta
        if fields:
            field_names = set(fields)
        elif exclude:
            excludeset = set(exclude)
            field_names = set([field.name for field in opts.fields])
            field_names = field_names - excludeset

        response = HttpResponse(mimetype='text/csv')
        response['Content-Disposition'] = ('attachment; filename=%s.csv' %
                                           unicode(opts).replace('.', '_'))

        writer = csv.writer(response, delimiter=';')
        if header:
            writer.writerow(list(field_names))
        for obj in queryset:
            row = []
            for field in field_names:
                # Process fields that refer to attributes of
                # foreignkeys, like 'user__email'.
                subfields = field.split('__')
                value = getattr(obj, subfields[0])
                for subfield in subfields[1:]:
                    value = getattr(value, subfield)
                row.append(unicode(value).encode('utf-8'))
            writer.writerow(row)
        return response

    export_as_csv.short_description = (description or 'Export to CSV file')
    return export_as_csv


def subscribe_to_basket_action():
    """Subscribe to Basket action."""

    def subscribe_to_basket(modeladmin, request, queryset):
        """Subscribe to Basket or update details of already subscribed."""
        ts = [(mozillians.users.tasks.update_basket_task
               .subtask(args=[userprofile.id]))
              for userprofile in queryset]
        TaskSet(ts).apply_async()
        messages.success(request, 'Basket update started.')
    subscribe_to_basket.short_description = 'Subscribe to or Update Basket'
    return subscribe_to_basket


def unsubscribe_from_basket_action():
    """Unsubscribe from Basket action."""

    def unsubscribe_from_basket(modeladmin, request, queryset):
        """Unsubscribe from Basket."""
        ts = [(mozillians.users.tasks.remove_from_basket_task
               .subtask(userprofile.user.email, userprofile.basket_token))
              for userprofile in queryset]
        TaskSet(ts).apply_async()
        messages.success(request, 'Basket update started.')
    unsubscribe_from_basket.short_description = 'Unsubscribe from Basket'
    return unsubscribe_from_basket


class SuperUserFilter(SimpleListFilter):
    """Admin filter for superusers."""
    title = 'has access to admin interface'
    parameter_name = 'superuser'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset

        value = self.value() == 'True'
        return queryset.filter(user__is_staff=value)


class PublicProfileFilter(SimpleListFilter):
    """Admin filter for public profiles."""
    title = 'public profile'
    parameter_name = 'public_profile'

    def lookups(self, request, model_admin):
        return (('False', 'No'),
                ('True', 'Yes'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset

        if self.value() == 'True':
            return queryset.filter(Q_PUBLIC_PROFILES)

        return queryset.exclude(Q_PUBLIC_PROFILES)


class CompleteProfileFilter(SimpleListFilter):
    """Admin filter for complete profiles."""
    title = 'complete profile'
    parameter_name = 'complete_profile'

    def lookups(self, request, model_admin):
        return (('False', 'Incomplete'),
                ('True', 'Complete'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        elif self.value() == 'True':
            return queryset.exclude(full_name='')
        else:
            return queryset.filter(full_name='')


class DateJoinedFilter(SimpleListFilter):
    """Admin filter for date joined."""
    title = 'date joined'
    parameter_name = 'date_joined'

    def lookups(self, request, model_admin):

        return map(lambda x: (str(x.year), x.year),
                   User.objects.dates('date_joined', 'year'))

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        else:
            return queryset.filter(user__date_joined__year=self.value())
        return queryset


class LastLoginFilter(SimpleListFilter):
    """Admin filter for last login."""
    title = 'last login'
    parameter_name = 'last_login'

    def lookups(self, request, model_admin):
        return (('<6', 'Less than 6 months'),
                ('>6', 'Between 6 and 12 months'),
                ('>12', 'More than a year'))

    def queryset(self, request, queryset):
        half_year = datetime.today() - timedelta(days=180)
        full_year = datetime.today() - timedelta(days=360)

        if self.value() == '<6':
            return queryset.filter(user__last_login__gte=half_year)
        elif self.value() == '>6':
            return queryset.filter(user__last_login__lt=half_year,
                                   user__last_login__gt=full_year)
        elif self.value() == '>12':
            return queryset.filter(user__last_login__lt=full_year)
        return queryset


class UserProfileInline(AdminImageMixin, admin.StackedInline):
    """UserProfile Inline model for UserAdmin."""
    model = UserProfile
    readonly_fields = ['date_vouched', 'vouched_by']
    form = autocomplete_light.modelform_factory(UserProfile)


class UsernameBlacklistAdmin(admin.ModelAdmin):
    """UsernameBlacklist Admin."""
    save_on_top = True
    search_fields = ['value']
    list_filter = ['is_regex']
    list_display = ['value', 'is_regex']


admin.site.register(UsernameBlacklist, UsernameBlacklistAdmin)


class LanguageAdmin(admin.ModelAdmin):
    search_fields = ['userprofile__full_name', 'userprofile__user__email', 'code']
    list_display = ['code', 'userprofile']
    list_filter = ['code']


admin.site.register(Language, LanguageAdmin)


class SkillInline(admin.TabularInline):
    model = Skill
    extra = 1


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 1
    form = autocomplete_light.modelform_factory(GroupMembership)


class LanguageInline(admin.TabularInline):
    model = Language
    extra = 1


class ExternalAccountInline(admin.TabularInline):
    model = ExternalAccount
    extra = 1


class UserProfileAdminForm(forms.ModelForm):
    username = forms.CharField()
    email = forms.CharField()

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.get('instance')
        if self.instance:
            self.base_fields['username'].initial = self.instance.user.username
            self.base_fields['email'].initial = self.instance.user.email
        super(UserProfileAdminForm, self).__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.instance:
            self.instance.user.username = self.cleaned_data.get('username')
            self.instance.user.email = self.cleaned_data.get('email')
            self.instance.user.save()
        return super(UserProfileAdminForm, self).save(*args, **kwargs)

    class Meta:
        model = UserProfile


class UserProfileAdmin(AdminImageMixin, admin.ModelAdmin):
    inlines = [LanguageInline, GroupMembershipInline, ExternalAccountInline]
    search_fields = ['full_name', 'user__email', 'user__username', 'ircname']
    readonly_fields = ['date_vouched', 'vouched_by', 'user']
    form = UserProfileAdminForm
    list_filter = ['is_vouched', DateJoinedFilter,
                   LastLoginFilter, SuperUserFilter, CompleteProfileFilter,
                   PublicProfileFilter]
    save_on_top = True
    list_display = ['full_name', 'email', 'username', 'country', 'is_vouched',
                    'vouched_by', 'number_of_vouchees']
    list_display_links = ['full_name', 'email', 'username']
    actions = [export_as_csv_action(fields=('user__username', 'user__email'), header=True),
               subscribe_to_basket_action(), unsubscribe_from_basket_action()]

    fieldsets = (
        ('Account', {
            'fields': ('full_name', 'username', 'email', 'photo',)
        }),
        (None, {
            'fields': ('title', 'bio', 'tshirt', 'ircname', 'date_mozillian',)
        }),
        ('Vouch Info', {
            'fields': ('date_vouched', 'is_vouched', 'vouched_by')
        }),
        ('Location', {
            'fields': ('country', 'region', 'city', 'timezone')
        }),
        ('Services', {
            'fields': ('allows_community_sites', 'allows_mozilla_sites')
        }),
        ('Privacy Settings', {
            'fields': ('privacy_photo', 'privacy_full_name', 'privacy_ircname',
                       'privacy_email', 'privacy_bio', 'privacy_city', 'privacy_region',
                       'privacy_country', 'privacy_groups', 'privacy_skills', 'privacy_languages',
                       'privacy_vouched_by', 'privacy_date_mozillian', 'privacy_timezone',
                       'privacy_tshirt', 'privacy_title'),
            'classes': ('collapse',)
        }),
        ('Basket', {
            'fields': ('basket_token',),
            'classes': ('collapse',)
        }),
        ('Skills', {
            'fields': ('skills',)
        }),
    )

    def queryset(self, request):
        qs = super(UserProfileAdmin, self).queryset(request)
        qs = qs.annotate(Count('vouchees'))
        return qs

    def email(self, obj):
        return obj.user.email
    email.admin_order_field = 'user__email'

    def username(self, obj):
        return obj.user.username
    username.admin_order_field = 'user__username'

    def country(self, obj):
        return COUNTRIES.get(obj.userprofile.country, '')
    country.admin_order_field = 'userprofile__country'

    def is_vouched(self, obj):
        return obj.userprofile.is_vouched
    is_vouched.boolean = True
    is_vouched.admin_order_field = 'is_vouched'

    def vouched_by(self, obj):
        voucher = obj.vouched_by
        voucher_url = reverse('admin:auth_user_change', args=[voucher.id])
        return '<a href="%s">%s</a>' % (voucher_url, voucher)
    vouched_by.admin_order_field = 'vouched_by'
    vouched_by.allow_tags = True

    def number_of_vouchees(self, obj):
        """Return the number of vouchees for obj."""
        return obj.vouchees.count()
    number_of_vouchees.admin_order_field = 'vouchees__count'

    def get_actions(self, request):
        """Return bulk actions for UserAdmin without bulk delete."""
        actions = super(UserProfileAdmin, self).get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def index_profiles(self, request):
        """Fire an Elastic Search Index Profiles task."""
        index_all_profiles()
        messages.success(request, 'Profile indexing started.')
        return HttpResponseRedirect(reverse('admin:users_userprofile_changelist'))

    def get_urls(self):
        """Return custom and UserProfileAdmin urls."""

        def wrap(view):

            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        urls = super(UserProfileAdmin, self).get_urls()
        my_urls = patterns('', url(r'index_profiles', wrap(self.index_profiles),
                                   name='users_index_profiles'))
        return my_urls + urls

admin.site.register(UserProfile, UserProfileAdmin)
