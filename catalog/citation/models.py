from django.contrib.auth.models import User
from django.contrib.postgres.fields import JSONField
from django.contrib.sites.requests import RequestSite
from django.core.urlresolvers import reverse
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.template import Context
from django.template.loader import get_template
from django.utils.translation import ugettext_lazy as _
from datetime import datetime, date
from collections import defaultdict
from django.core import serializers
import re

from model_utils import Choices
from model_utils.managers import InheritanceManager
from typing import Dict, Optional


class LogManager(models.Manager):
    # TODO: add log_bulk_create method

    use_for_related_fields = True

    def log_create(self, audit_command, **kwargs):
        with transaction.atomic():
            instance = self.create(**kwargs)
            AuditLog.objects.create(
                action='INSERT',
                row_id=instance.id,
                table=instance._meta.db_table,
                payload={},
                audit_command=audit_command)
            return instance

    def log_get_or_create(self, audit_command, **kwargs):
        defaults = kwargs.pop('defaults', {})
        with transaction.atomic():
            instance, created = self.get_or_create(defaults=defaults, **kwargs)
            if created:
                action = 'INSERT'
                payload = {}
                row_id = instance.id
            else:
                defaults.update(kwargs)
                action = 'UPDATE'
                payload = {column: getattr(instance, column)
                           for column in defaults.keys()}
                row_id = instance.id
            AuditLog.objects.create(
                action=action,
                row_id=row_id,
                table=instance._meta.db_table,
                payload=payload,
                audit_command=audit_command)

        return instance, created


def datetime_json_serialize(datetime_obj: Optional[datetime]):
    if datetime_obj:
        val = {'tag': 'just',
               'value': {
                   'year': datetime_obj.year,
                   'month': datetime_obj.month,
                   'day': datetime_obj.day,
                   'hour': datetime_obj.hour,
                   'minute': datetime_obj.minute,
                   'microsecond': datetime_obj.microsecond,
                   'tzinfo': datetime_obj.tzinfo.zone}
               }
    else:
        val = {'tag': 'nothing'}
    return val


def date_json_serialize(date_obj: Optional[date]):
    if date_obj is not None:
        val = {'tag': 'just',
               'value': {
                   'year': date_obj.year,
                   'month': date_obj.month,
                   'day': date_obj.day
               }}
    else:
        val = {'tag': 'nothing'}
    return val


def identity_json_serialize(obj):
    return obj


class LogQuerySet(models.query.QuerySet):
    # TODO: get of serializers and use a custom encode
    # serializers.serialize('json', [p])
    DISPATCH_JSON_SERIALIZE = defaultdict(lambda: identity_json_serialize,
                                          DateTimeField=datetime_json_serialize,
                                          DateField=date_json_serialize)

    def json_serialize(self, field_type, obj):
        return LogQuerySet.DISPATCH_JSON_SERIALIZE[field_type](obj)

    def log_delete(self, audit_command):
        with transaction.atomic():
            instances = self.all()

            auditlogs = []
            for instance in instances:
                payload = {field.column: self.json_serialize(field.get_internal_type(), getattr(instance, field.column))
                           for field in instance._meta.local_fields}
                auditlogs.append(
                    AuditLog(
                        action='DELETE',
                        row_id=instance.id,
                        table=instance._meta.db_table,
                        payload=payload,
                        audit_command=audit_command))
            AuditLog.objects.bulk_create(auditlogs)

            return instances.delete()

    def log_update(self, audit_command, **kwargs):
        # TODO: call instance.log_update in loop here
        with transaction.atomic():
            instances = self.all()

            auditlogs = []
            for instance in instances:
                original_values = {column: getattr(instance, column)
                                   for column in kwargs.keys()}
                row_id = instance.id
                auditlogs.append(
                    AuditLog(
                        action='UPDATE',
                        row_id=row_id,
                        table=instance._meta.db_table,
                        payload=original_values,
                        audit_command=audit_command))
            AuditLog.objects.bulk_create(auditlogs)

            return self.update(**kwargs)


class AbstractLogModel(models.Model):
    def json_serialize(self, field_type, obj):
        return LogQuerySet.DISPATCH_JSON_SERIALIZE[field_type](obj)

    def log_delete(self, audit_command):
        with transaction.atomic():
            payload = {field.column: self.json_serialize(field.get_internal_type(), getattr(self, field.column))
                       for field in self._meta.local_fields}
            auditlogs = \
                AuditLog.objects.create(
                    action='DELETE',
                    row_id=self.id,
                    table=self._meta.db_table,
                    payload=payload,
                    audit_command=audit_command)
            info = self.delete()
            return info

    # TODO: replace with log_update
    def log_save(self, audit_command, payload: Optional[Dict] = None):
        with transaction.atomic():
            auditlog = AuditLog(
                table=self._meta.db_table,
                audit_command=audit_command)
            if payload:
                assert self.id is not None
                auditlog.row_id = self.id
                auditlog.action = 'UPDATE'
                auditlog.payload = payload
                info = self.save()
            else:
                info = self.save()
                auditlog.action = 'INSERT'
                auditlog.row_id = self.id
                auditlog.payload = {}

            auditlog.save()
            return info

    objects = LogManager.from_queryset(LogQuerySet)()

    class Meta:
        abstract = True


class InvitationEmail(object):
    def __init__(self, request):
        self.request = request
        self.plaintext_template = get_template('email/invitation-email.txt')

    @property
    def site(self):
        return RequestSite(self.request)

    def get_context(self, message, token):
        return Context({
            'invitation_text': message,
            'domain': self.site.domain,
            'token': token,
        })

    def get_plaintext_content(self, message, token):
        return self.plaintext_template.render(self.get_context(message, token))


class InvitationEmailTemplate(models.Model):
    name = models.CharField(max_length=32)
    text = models.TextField()
    date_added = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    added_by = models.ForeignKey(User, related_name="citation_added_by")


class Author(AbstractLogModel):
    # TODO: rename primary_* name fields to not start with primary
    INDIVIDUAL = 'INDIVIDUAL'
    ORGANIZATION = 'ORGANIZATION'
    TYPE_CHOICES = Choices(
        (INDIVIDUAL, _('individual')),
        (ORGANIZATION, _('organization')),
    )
    type = models.TextField(choices=TYPE_CHOICES, max_length=32)
    primary_given_name = models.CharField(max_length=200)
    primary_family_name = models.CharField(max_length=200)
    orcid = models.TextField(max_length=200)
    email = models.EmailField(blank=True)

    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this model was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this model was last modified on this system'))

    def __repr__(self):
        return "Author(orcid={orcid}. primary_given_name={primary_given_name}, primary_family_name={primary_family_name})" \
            .format(orcid=self.orcid, primary_given_name=self.primary_given_name,
                    primary_family_name=self.primary_family_name)

    @staticmethod
    def normalize_author_name(author_str: str):
        normalized_name = author_str.upper()
        normalized_name = re.sub(r"\n|\r", " ", normalized_name)
        normalized_name = re.sub(r"\.|,|\{|\}", "", normalized_name)
        normalized_name_split = normalized_name.split(' ', 1)
        if len(normalized_name_split) == 2:
            family, given = normalized_name_split
        else:
            family, given = normalized_name, ''
        return family, given


class AuthorAlias(AbstractLogModel):
    # Authors that are not an individual only have a given name
    given_name = models.CharField(max_length=200)
    family_name = models.CharField(max_length=200)

    author = models.ForeignKey(Author, on_delete=models.PROTECT, related_name="author_aliases")

    @property
    def name(self):
        if self.family_name:
            if self.given_name:
                return self.given_name + ' ' + self.family_name
            else:
                return self.family_name
        return self.given_name

    def __repr__(self):
        return "AuthorAlias(id={id}, family_name={family_name}, given_name={given_name}, author_id={author_id})" \
            .format(id=self.id, family_name=self.family_name, given_name=self.given_name, author_id=self.author_id)

    class Meta:
        unique_together = ('author', 'given_name', 'family_name')


class Tag(AbstractLogModel):
    name = models.CharField(max_length=255, unique=True)
    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this model was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this model was last modified on this system'))

    def __unicode__(self):
        return self.name


class ModelDocumentation(AbstractLogModel):
    CATEGORIES = [
        {'category': 'Narrative',
         'modelDocumentationList': [{'category': 'Narrative', 'name': 'ODD'},
                                    {'category': 'Narrative', 'name': 'Other Narrative'}]},
        {'category': 'Visual Relationships',
         'modelDocumentationList': [{'category': 'Visual Relationships', 'name': 'UML'},
                                    {'category': 'Visual Relationships', 'name': 'Flow charts'},
                                    {'category': 'Visual Relationships', 'name': 'Ontologies'},
                                    {'category': 'Visual Relationships', 'name': 'AORML'}]},
        {'category': 'Code and formal descriptions',
         'modelDocumentationList': [{'category': 'Code and formal descriptions', 'name': 'Source code'},
                                    {'category': 'Code and formal descriptions', 'name': 'Pseudocode'},
                                    {'category': 'Code and formal descriptions', 'name': 'Mathematical description'}]},
    ]
    ''' common choices: UML, ODD, Word / PDF doc '''
    name = models.CharField(max_length=255, unique=True)
    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this model was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this model was last modified on this system'))

    def __unicode__(self):
        return self.name


class Note(AbstractLogModel):
    text = models.TextField()
    date_added = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)
    zotero_key = models.CharField(max_length=64, null=True, unique=True, blank=True)
    zotero_date_added = models.DateTimeField(null=True, blank=True)
    zotero_date_modified = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(User, related_name='citation_added_note_set')
    deleted_on = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, related_name='citation_deleted_note_set', null=True, blank=True)
    publication = models.ForeignKey('Publication', null=True, blank=True)

    @property
    def is_deleted(self):
        return bool(self.deleted_on)

    def __unicode__(self):
        return self.text


class Platform(AbstractLogModel):
    """ model platform, e.g, NetLogo or RePast """
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(default='', blank=True)
    description = models.TextField(default='', blank=True)
    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this model was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this model was last modified on this system'))

    def __unicode__(self):
        return self.name


class Sponsor(AbstractLogModel):
    """ funding agency sponsoring this research """
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(default='', blank=True)
    description = models.TextField(default='', blank=True)
    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this model was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this model was last modified on this system'))

    def __unicode__(self):
        return self.name


class Container(AbstractLogModel):
    """Canonical Container"""
    issn = models.TextField(max_length=500, blank=True, default='')
    type = models.TextField(max_length=1000, blank=True, default='')
    primary_name = models.CharField(max_length=300)

    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this container was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this container was last modified on this system'))

    def __repr__(self):
        return "Container(issn={issn}, type={type})" \
            .format(issn=self.issn, type=self.type)


class ContainerAlias(AbstractLogModel):
    name = models.TextField(max_length=1000, blank=True, default='')
    container = models.ForeignKey(Container, on_delete=models.PROTECT, related_name="container_aliases")

    def __repr__(self):
        return "ContainerAlias(name={name}, container={container})" \
            .format(name=self.name, container=self.container)

    class Meta:
        unique_together = ('container', 'name')


class Publication(AbstractLogModel):
    Status = Choices(
        ('UNTAGGED', _('Not reviewed')),
        ('NEEDS_AUTHOR_REVIEW', _('Curator has reviewed publication, requires author intervention.')),
        ('FLAGGED', _('Flagged for further internal review by CoMSES staff')),
        ('AUTHOR_UPDATED', _('Updated by author, needs CoMSES review')),
        ('INVALID', _('Publication record is not applicable or invalid')),
        ('COMPLETE', _('Reviewed and verified by CoMSES')),
    )

    # zotero publication metadata
    title = models.TextField()
    abstract = models.TextField(blank=True)
    short_title = models.CharField(max_length=255, blank=True)
    zotero_key = models.CharField(max_length=64, null=True, unique=True, blank=True)
    url = models.URLField(blank=True)
    date_published_text = models.CharField(max_length=32, blank=True)
    date_published = models.DateField(null=True, blank=True)
    date_accessed = models.DateField(null=True, blank=True)
    archive = models.CharField(max_length=255, blank=True)
    archive_location = models.CharField(max_length=255, blank=True)
    library_catalog = models.CharField(max_length=255, blank=True)
    call_number = models.CharField(max_length=255, blank=True)
    rights = models.CharField(max_length=255, blank=True)
    extra = models.TextField(blank=True)
    published_language = models.CharField(max_length=255, default='English', blank=True)
    zotero_date_added = models.DateTimeField(help_text=_('date added field from zotero'), null=True, blank=True)
    zotero_date_modified = models.DateTimeField(help_text=_('date modified field from zotero'), null=True, blank=True)
    creators = models.ManyToManyField(Author, related_name='publications', through='PublicationAuthors')

    # custom incoming tags set by zotero data entry to mark the code archive url, contact author's email, the ABM platform
    # used, research sponsors (funding agencies, etc.), documentation, and other research keyword tags
    code_archive_url = models.URLField(max_length=255, blank=True)
    contact_author_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    platforms = models.ManyToManyField(Platform, blank=True, through='PublicationPlatforms',
                                       related_name='publications')
    sponsors = models.ManyToManyField(Sponsor, blank=True, through='PublicationSponsors', related_name='publications')
    model_documentation = models.ManyToManyField(ModelDocumentation, through='PublicationModelDocumentations',
                                                 blank=True, related_name='publications')
    tags = models.ManyToManyField(Tag, through='PublicationTags', blank=True)
    added_by = models.ForeignKey(User, related_name='citation_added_publication_set')

    # custom fields used by catalog internally
    status = models.CharField(choices=Status, max_length=32, default=Status.UNTAGGED)
    date_added = models.DateTimeField(auto_now_add=True,
                                      help_text=_('Date this publication was imported into this system'))
    date_modified = models.DateTimeField(auto_now=True,
                                         help_text=_('Date this publication was last modified on this system'))

    author_comments = models.TextField(blank=True)
    email_sent_count = models.PositiveIntegerField(default=0)
    assigned_curator = models.ForeignKey(User,
                                         null=True,
                                         blank=True,
                                         help_text=_("Currently assigned curator"),
                                         related_name='citation_assigned_publication_set')

    # type fields
    is_primary = models.BooleanField(default=True)

    # container specific fields
    container = models.ForeignKey(Container, null=True, blank=True, on_delete=models.SET_NULL)
    pages = models.CharField(max_length=255, default='', blank=True)
    issn = models.CharField(max_length=255, default='', blank=True)
    volume = models.CharField(max_length=255, default='', blank=True)
    issue = models.CharField(max_length=255, default='', blank=True)
    series = models.CharField(max_length=255, default='', blank=True)
    series_title = models.CharField(max_length=255, default='', blank=True)
    series_text = models.CharField(max_length=255, default='', blank=True)
    doi = models.CharField(max_length=255, default='', blank=True)

    citations = models.ManyToManyField(
        "self", symmetrical=False, related_name="referenced_by",
        through='PublicationCitations', through_fields=('publication', 'citation'))

    def is_editable_by(self, user):
        # eventually consider having permission groups or per-object permissions
        return self.assigned_curator == user

    # TODO: replace primary key lookup with slug
    def _pk_url(self, name):
        return reverse(name, args=[self.pk])

    def get_absolute_url(self):
        return self._pk_url('citation:publication_detail')

    def get_curator_url(self):
        return self._pk_url('citation:curator_publication_detail')

    def __str__(self):
        return "Primary: {}; Date Published: {}; Title: {}; DOI: {}" \
            .format(self.is_primary,
                    self.date_published_text,
                    self.title,
                    self.doi)


class AuditCommand(models.Model):
    Role = Choices(('AUTHOR_EDIT', _('Author edit')),
                   ('SYSTEM_LOG', _('System log')),
                   ('CURATOR_EDIT', _('Curator edit')))
    Action = Choices(('SPLIT', _('Split Record')),
                     ('MERGE', _('Merge Records')),
                     ('LOAD', _('Load from File')),
                     ('MANUAL', _('User entered changes')))

    role = models.CharField(max_length=32, choices=Role)
    action = models.CharField(max_length=32, choices=Action)
    date_added = models.DateTimeField(auto_now_add=True)
    creator = models.ForeignKey(User, null=True, blank=True, related_name="citation_creator_set",
                                help_text=_('The user who initiated this action, if any.'))
    message = models.TextField(blank=True, help_text=_('A human readable representation of the change made'))

    class Meta:
        ordering = ['-date_added']


class AuditLog(models.Model):
    # TODO: may want to add a generic foreign key to table, row_id combination
    Action = Choices(('UPDATE', _('Update')),
                     ('INSERT', _('Insert')),
                     ('DELETE', _('Delete')))
    action = models.CharField(max_length=32, choices=Action)
    row_id = models.BigIntegerField()
    table = models.CharField(max_length=128)
    payload = JSONField(blank=True, null=True,
                        help_text=_('A JSON dictionary containing modified fields, if any, for the given publication'))
    audit_command = models.ForeignKey(AuditCommand)

    def __str__(self):
        return u"{} - {} performed {} on {}".format(
            self.creator,
            self.action,
            self.message,
            self.payload,
        )

    class Meta:
        ordering = ['-id']


class AbstractWithDateAddedModel:
    date_added = models.DateTimeField(auto_now=True)


class Raw(AbstractLogModel, AbstractWithDateAddedModel):
    BIBTEX_FILE = "BIBTEX_FILE"
    BIBTEX_ENTRY = "BIBTEX_ENTRY"
    BIBTEX_REF = "BIBTEX_REF"
    CROSSREF_DOI_SUCCESS = "CROSSREF_DOI_SUCCESS"
    CROSSREF_DOI_FAIL = "CROSSREF_DOI_FAIL"
    CROSSREF_SEARCH_SUCCESS = "CROSSREF_SEARCH_SUCCESS"
    CROSSREF_SEARCH_FAIL_NOT_UNIQUE = "CROSSREF_SEARCH_FAIL_NOT_UNIQUE"
    CROSSREF_SEARCH_FAIL_OTHER = "CROSSREF_SEARCH_FAIL_OTHER"
    CROSSREF_SEARCH_CANDIDATE = "CROSSREF_SEARCH_CANDIDATE"

    SOURCE_CHOICES = Choices(
        (BIBTEX_FILE, "BibTeX File"),
        (BIBTEX_ENTRY, "BibTeX Entry"),
        (BIBTEX_REF, "BibTeX Reference"),
        (CROSSREF_DOI_SUCCESS, "CrossRef lookup succeeded"),
        (CROSSREF_DOI_FAIL, "CrossRef lookup failed"),
        (CROSSREF_SEARCH_SUCCESS, "CrossRef search succeeded"),
        (CROSSREF_SEARCH_FAIL_NOT_UNIQUE, "CrossRef search failed - not unique"),
        (CROSSREF_SEARCH_FAIL_OTHER, "CrossRef search failed - other"),
        (CROSSREF_SEARCH_CANDIDATE, "CrossRef search match candidate")
    )
    key = models.TextField(choices=SOURCE_CHOICES, max_length=100)
    value = JSONField()

    publication = models.ForeignKey(Publication, related_name='raw', on_delete=models.PROTECT)
    container = models.ForeignKey(Container, related_name='raw', on_delete=models.PROTECT)
    authors = models.ManyToManyField(Author, related_name='raw', through='RawAuthors')


class PublicationAuthors(AbstractLogModel, AbstractWithDateAddedModel):
    RoleChoices = Choices(
        ('AUTHOR', _('author')),
        ('REVIEWED_AUTHOR', _('reviewed author')),
        ('CONTRIBUTOR', _('contributor')),
        ('EDITOR', _('editor')),
        ('TRANSLATOR', _('translator')),
        ('SERIES_EDITOR', _('series editor')),
    )
    publication = models.ForeignKey(Publication, related_name='publication_authors')
    author = models.ForeignKey(Author, related_name='publication_authors')
    role = models.CharField(choices=RoleChoices, max_length=32)

    class Meta:
        unique_together = ('publication', 'author')


class PublicationCitations(AbstractLogModel, AbstractWithDateAddedModel):
    publication = models.ForeignKey(Publication, related_name='publication_citations')
    citation = models.ForeignKey(Publication, related_name='publication_citations_referenced_by')

    class Meta:
        unique_together = ('publication', 'citation')


class PublicationModelDocumentations(AbstractLogModel, AbstractWithDateAddedModel):
    publication = models.ForeignKey(Publication, related_name='publication_modeldocumentations')
    model_documentation = models.ForeignKey(ModelDocumentation, related_name='publication_modeldocumentations')

    class Meta:
        unique_together = ('publication', 'model_documentation')


class PublicationPlatforms(AbstractLogModel, AbstractWithDateAddedModel):
    publication = models.ForeignKey(Publication, related_name='publication_platforms')
    platform = models.ForeignKey(Platform, related_name='publications_platforms')

    class Meta:
        unique_together = ('publication', 'platform')


class PublicationSponsors(AbstractLogModel, AbstractWithDateAddedModel):
    publication = models.ForeignKey(Publication, related_name='publication_sponsors')
    sponsor = models.ForeignKey(Sponsor, related_name='publication_sponsors')

    class Meta:
        unique_together = ('publication', 'sponsor')


class PublicationTags(AbstractLogModel, AbstractWithDateAddedModel):
    publication = models.ForeignKey(Publication, related_name='publication_tags')
    tag = models.ForeignKey(Tag, related_name='publication_tags')

    class Meta:
        unique_together = ('publication', 'tag')


class RawAuthors(AbstractLogModel, AbstractWithDateAddedModel):
    author = models.ForeignKey(Author, related_name='raw_authors')
    raw = models.ForeignKey(Raw, related_name='raw_authors')

    class Meta:
        unique_together = ('author', 'raw')