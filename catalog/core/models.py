from django.contrib.auth.models import User
from django.contrib.sites.models import Site, RequestSite
from django.core.urlresolvers import reverse
from django.db import models
from django.template import Context
from django.template.loader import get_template
from django.utils.translation import ugettext_lazy as _

from model_utils import Choices
from model_utils.managers import InheritanceManager


class InvitationEmail(object):

    def __init__(self, request):
        self.request = request
        self.plaintext_template = get_template('email/invitation-email.txt')

    @property
    def site(self):
        if Site._meta.installed:
            return Site.objects.get_current()
        else:
            return RequestSite(self.request)

    @property
    def is_secure(self):
        return self.request.is_secure()

    def get_context(self, message, token):
        return Context({
            'invitation_text': message,
            'site': self.site,
            'token': token,
            'secure': self.is_secure
        })

    def get_plaintext_content(self, message, token):
        return self.plaintext_template.render(self.get_context(message, token))


class InvitationEmailTemplate(models.Model):
    name = models.CharField(max_length=32)
    text = models.TextField()
    date_added = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    added_by = models.ForeignKey(User)


class Creator(models.Model):
    CREATOR_CHOICES = Choices(
        ('AUTHOR', _('author')),
        ('REVIEWED_AUTHOR', _('reviewed author')),
        ('CONTRIBUTOR', _('contributor')),
        ('EDITOR', _('editor')),
        ('TRANSLATOR', _('translator')),
        ('SERIES_EDITOR', _('series editor')),
    )
    creator_type = models.CharField(choices=CREATOR_CHOICES, max_length=32)
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)

    def __unicode__(self):
        return u'{} {} ({})'.format(self.first_name, self.last_name, self.creator_type)


class Tag(models.Model):
    name = models.CharField(max_length=255, unique=True)

    def __unicode__(self):
        return self.name


class ModelDocumentation(models.Model):
    ''' common choices: UML, ODD, Word / PDF doc '''
    name = models.CharField(max_length=255, unique=True)

    def __unicode__(self):
        return self.name


class Note(models.Model):
    text = models.TextField()
    date_added = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)
    zotero_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    zotero_date_added = models.DateTimeField(null=True, blank=True)
    zotero_date_modified = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(User, related_name='added_note_set')
    deleted_on = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(User, related_name='deleted_note_set', null=True, blank=True)

    publication = models.ForeignKey('Publication', null=True, blank=True)

    def __unicode__(self):
        return u'{}'.format(self.text)


class Platform(models.Model):
    """ model platform, e.g, NetLogo or RePast """
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    def __unicode__(self):
        return u'{}'.format(self.name)


class PlatformVersion(models.Model):
    platform = models.ForeignKey(Platform)
    version = models.TextField()


class Sponsor(models.Model):
    """ funding agency sponsoring this research """
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)

    def __unicode__(self):
        return u'{}'.format(self.name)


class Publication(models.Model):
    Status = Choices(
        ('UNTAGGED', _('Not reviewed')),
        ('NEEDS_AUTHOR_REVIEW', _('Curator has reviewed publication, requires author intervention.')),
        ('FLAGGED', _('Flagged for further internal review by CoMSES staff')),
        ('AUTHOR_UPDATED', _('Updated by author, needs CoMSES review')),
        ('INVALID', _('Publication record is not applicable or invalid')),
        ('COMPLETE', _('Reviewed and verified by CoMSES')),
    )
    objects = InheritanceManager()

# zotero publication metadata
    title = models.TextField()
    abstract = models.TextField(blank=True)
    short_title = models.CharField(max_length=255, blank=True)
    zotero_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    url = models.URLField(null=True, blank=True)
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
    creators = models.ManyToManyField(Creator)

# custom incoming tags set by zotero data entry to mark the code archive url, contact author's email, the ABM platform
# used, research sponsors (funding agencies, etc.), documentation, and other research keyword tags
    code_archive_url = models.URLField(max_length=255, null=True, blank=True)
    contact_author_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    platforms = models.ManyToManyField(Platform, blank=True)
    sponsors = models.ManyToManyField(Sponsor, blank=True)
    model_documentation = models.ForeignKey(ModelDocumentation, null=True, blank=True)
    tags = models.ManyToManyField(Tag, blank=True)
    added_by = models.ForeignKey(User, related_name='added_publication_set')

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
                                         related_name='assigned_publication_set')

    def get_absolute_url(self):
        return reverse('core:publication_detail', args=[self.pk])

    def __unicode__(self):
        return u'{}'.format(self.title)


class Journal(models.Model):
    name = models.CharField(max_length=255, unique=True)
    url = models.URLField(max_length=255, null=True, blank=True)
    abbreviation = models.CharField(max_length=255, blank=True)

    def __unicode__(self):
        return u'{}'.format(self.name)


class JournalArticle(Publication):
    journal = models.ForeignKey(Journal)
    pages = models.CharField(max_length=255, blank=True)
    issn = models.CharField(max_length=255, blank=True)
    volume = models.CharField(max_length=255, blank=True)
    issue = models.CharField(max_length=255, blank=True)
    series = models.CharField(max_length=255, blank=True)
    series_title = models.CharField(max_length=255, blank=True)
    series_text = models.CharField(max_length=255, blank=True)
    doi = models.CharField(max_length=255, blank=True)


class PublicationAuditLog(models.Model):
    Action = Choices(('AUTHOR_EDIT', _('Author edit')),
                     ('SYSTEM_LOG', _('System log')),
                     ('CURATOR_EDIT', _('Curator edit')))
    date_added = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=32, choices=Action, default=Action.SYSTEM_LOG)
    publication = models.ForeignKey(Publication)
    message = models.TextField(blank=True)
    creator = models.ForeignKey(User, null=True, blank=True, help_text=_('The user who initiated this action, if any.'))

"""
class Book(Publication):
    series = models.CharField(max_length=255, blank=True)
    series_number = models.CharField(max_length=32, blank=True)
    volume = models.CharField(max_length=32, blank=True)
    num_of_volume = models.CharField(max_length=32, blank=True)
    edition = models.CharField(max_length=32, blank=True)
    place = models.CharField(max_length=32, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    num_of_pages = models.CharField(max_length=32, blank=True)
    isbn = models.CharField(max_length=255, blank=True)

class Report(Publication):
    report_number = models.PositiveIntegerField(null=True, blank=True)
    report_type = models.CharField(max_length=255)
    series_title = models.CharField(max_length=255)
    place = models.CharField(max_length=255)
    institution = models.CharField(max_length=255)
    pages = models.PositiveIntegerField(null=True, blank=True)


class Thesis(Publication):
    thesis_type = models.CharField(max_length=255)
    university = models.CharField(max_length=255)
    place = models.CharField(max_length=255)
    num_of_pages = models.PositiveIntegerField(null=True, blank=True)
"""
