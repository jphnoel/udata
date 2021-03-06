# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from copy import copy
from datetime import datetime
from itertools import chain
from time import time

from blinker import Signal
from flask import url_for, current_app
from flask_security import UserMixin, RoleMixin, MongoEngineUserDatastore
from mongoengine.signals import pre_save, post_save
from itsdangerous import JSONWebSignatureSerializer

from werkzeug import cached_property

from udata import mail
from udata.frontend.markdown import mdstrip
from udata.i18n import lazy_gettext as _
from udata.models import db, WithMetrics, Follow
from udata.core.discussions.models import Discussion
from udata.core.storages import avatars, default_image_basename

__all__ = ('User', 'Role', 'datastore')

AVATAR_SIZES = [100, 32, 25]


# TODO: use simple text for role
class Role(db.Document, RoleMixin):
    ADMIN = 'admin'
    name = db.StringField(max_length=80, unique=True)
    description = db.StringField(max_length=255)

    def __str__(self):
        return self.name

    __unicode__ = __str__


class UserSettings(db.EmbeddedDocument):
    prefered_language = db.StringField()


class User(db.Document, WithMetrics, UserMixin):
    slug = db.SlugField(
        max_length=255, required=True, populate_from='fullname')
    email = db.StringField(max_length=255, required=True, unique=True)
    password = db.StringField()
    active = db.BooleanField()
    roles = db.ListField(db.ReferenceField(Role), default=[])

    first_name = db.StringField(max_length=255, required=True)
    last_name = db.StringField(max_length=255, required=True)

    avatar_url = db.URLField()
    avatar = db.ImageField(
        fs=avatars, basename=default_image_basename, thumbnails=AVATAR_SIZES)
    website = db.URLField()
    about = db.StringField()

    prefered_language = db.StringField()

    apikey = db.StringField()

    created_at = db.DateTimeField(default=datetime.now, required=True)

    # The field below is required for Flask-security
    # when SECURITY_CONFIRMABLE is True
    confirmed_at = db.DateTimeField()

    # The 5 fields below are required for Flask-security
    # when SECURITY_TRACKABLE is True
    last_login_at = db.DateTimeField()
    current_login_at = db.DateTimeField()
    last_login_ip = db.StringField()
    current_login_ip = db.StringField()
    login_count = db.IntField()

    deleted = db.DateTimeField()
    ext = db.MapField(db.GenericEmbeddedDocumentField())
    extras = db.ExtrasField()

    before_save = Signal()
    after_save = Signal()
    on_create = Signal()
    on_update = Signal()
    before_delete = Signal()
    after_delete = Signal()
    on_delete = Signal()

    meta = {
        'allow_inheritance': True,
        'indexes': ['-created_at', 'slug', 'apikey'],
        'ordering': ['-created_at']
    }

    def __str__(self):
        return self.fullname

    __unicode__ = __str__

    @property
    def fullname(self):
        return ' '.join((self.first_name or '', self.last_name or '')).strip()

    @cached_property
    def organizations(self):
        from udata.core.organization.models import Organization
        return Organization.objects(members__user=self)

    @property
    def sysadmin(self):
        return self.has_role('admin')

    def url_for(self, *args, **kwargs):
        return url_for('users.show', user=self, *args, **kwargs)

    display_url = property(url_for)

    @property
    def external_url(self):
        return self.url_for(_external=True)

    @property
    def visible(self):
        count = self.metrics.get('datasets', 0) + self.metrics.get('reuses', 0)
        return count > 0 and self.active

    @cached_property
    def resources_availability(self):
        """Return the percentage of availability for resources."""
        # Flatten the list.
        availabilities = list(
            chain(
                *[org.check_availability() for org in self.organizations]
            )
        )
        if availabilities:
            # Trick will work because it's a sum() of booleans.
            return round(100. * sum(availabilities) / len(availabilities), 2)
        else:
            return 0

    @cached_property
    def datasets_org_count(self):
        """Return the number of datasets of user's organizations."""
        from udata.models import Dataset  # Circular imports.
        return sum(Dataset.objects(organization=org).visible().count()
                   for org in self.organizations)

    @cached_property
    def followers_org_count(self):
        """Return the number of followers of user's organizations."""
        from udata.models import Follow  # Circular imports.
        return sum(Follow.objects(following=org).count()
                   for org in self.organizations)

    @property
    def datasets_count(self):
        """Return the number of datasets of the user."""
        return self.metrics.get('datasets', 0)

    @property
    def followers_count(self):
        """Return the number of followers of the user."""
        return self.metrics.get('followers', 0)

    def generate_api_key(self):
        s = JSONWebSignatureSerializer(current_app.config['SECRET_KEY'])
        self.apikey = s.dumps({
            'user': str(self.id),
            'time': time(),
        })

    def clear_api_key(self):
        self.apikey = None

    @classmethod
    def get(cls, id_or_slug):
        obj = cls.objects(slug=id_or_slug).first()
        return obj or cls.objects.get_or_404(id=id_or_slug)

    @classmethod
    def pre_save(cls, sender, document, **kwargs):
        cls.before_save.send(document)

    @classmethod
    def post_save(cls, sender, document, **kwargs):
        cls.after_save.send(document)
        if kwargs.get('created'):
            cls.on_create.send(document)
        else:
            cls.on_update.send(document)

    @cached_property
    def json_ld(self):

        result = {
            '@type': 'Person',
            '@context': 'http://schema.org',
            'name': self.fullname,
        }

        if self.about:
            result['description'] = mdstrip(self.about)

        if self.avatar_url:
            result['image'] = self.avatar_url

        if self.website:
            result['url'] = self.website

        return result

    def mark_as_deleted(self):
        copied_user = copy(self)
        self.email = '{}@deleted'.format(self.id)
        self.password = None
        self.active = False
        self.first_name = 'DELETED'
        self.last_name = 'DELETED'
        self.avatar = None
        self.avatar_url = None
        self.website = None
        self.about = None
        self.deleted = datetime.now()
        self.save()
        for organization in self.organizations:
            organization.members = [member
                                    for member in organization.members
                                    if member.user != self]
            organization.save()
        for discussion in Discussion.objects(discussion__posted_by=self):
            for message in discussion.discussion:
                if message.posted_by == self:
                    message.content = 'DELETED'
            discussion.save()
        Follow.objects(follower=self).delete()
        Follow.objects(following=self).delete()
        mail.send(_('Account deletion'), copied_user, 'account_deleted')


datastore = MongoEngineUserDatastore(db, User, Role)


pre_save.connect(User.pre_save, sender=User)
post_save.connect(User.post_save, sender=User)
