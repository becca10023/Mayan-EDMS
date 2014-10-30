from __future__ import absolute_import

from ast import literal_eval
import base64
import hashlib
import logging
import os
import tempfile

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext
from django.utils.translation import ugettext_lazy as _

from acls.utils import apply_default_acls
from common.settings import TEMPORARY_DIRECTORY
from converter.api import (convert, get_page_count,
                           get_available_transformations_choices)
from converter.exceptions import UnknownFileFormat
from converter.literals import (DEFAULT_ZOOM_LEVEL, DEFAULT_ROTATION,
                                DEFAULT_PAGE_NUMBER)
from history.api import create_history
from mimetype.api import get_mimetype

from .events import HISTORY_DOCUMENT_CREATED
from .exceptions import NewDocumentVersionNotAllowed
from .literals import (LANGUAGE_CHOICES, VERSION_UPDATE_MAJOR,
                       VERSION_UPDATE_MICRO, VERSION_UPDATE_MINOR)
from .managers import (DocumentManager, DocumentPageTransformationManager,
                       DocumentTypeManager, RecentDocumentManager)
from .runtime import storage_backend
from .settings import (CACHE_PATH, CHECKSUM_FUNCTION, DISPLAY_SIZE, LANGUAGE,
                       UUID_FUNCTION, ZOOM_MAX_LEVEL, ZOOM_MIN_LEVEL)
from .signals import post_version_upload, post_document_type_change

HASH_FUNCTION = lambda x: hashlib.sha256(x).hexdigest()  # document image cache name hash function
logger = logging.getLogger(__name__)


class DocumentType(models.Model):
    """
    Define document types or classes to which a specific set of
    properties can be attached
    """
    name = models.CharField(max_length=32, verbose_name=_(u'Name'), unique=True)

    # TODO: find a way to move this to the ocr app
    ocr = models.BooleanField(default=True, verbose_name=_(u'Automatically queue newly created documents for OCR.'))

    objects = DocumentTypeManager()

    def __unicode__(self):
        return self.name

    def natural_key(self):
        return (self.name,)

    class Meta:
        verbose_name = _(u'Document type')
        verbose_name_plural = _(u'Documents types')
        ordering = ['name']


class Document(models.Model):
    """
    Defines a single document with it's fields and properties
    """

    uuid = models.CharField(default=lambda: UUID_FUNCTION(), max_length=48, editable=False)
    document_type = models.ForeignKey(DocumentType, verbose_name=_(u'Document type'), related_name='documents')
    label = models.CharField(max_length=255, default=_('Uninitialized document'), db_index=True, verbose_name=_('Label'))
    description = models.TextField(blank=True, null=True, verbose_name=_(u'Description'))
    date_added = models.DateTimeField(verbose_name=_(u'Added'), auto_now_add=True)
    language = models.CharField(choices=LANGUAGE_CHOICES, default=LANGUAGE, max_length=8, verbose_name=_('Language'))

    objects = DocumentManager()

    class Meta:
        verbose_name = _(u'Document')
        verbose_name_plural = _(u'Documents')
        ordering = ['-date_added']

    def set_document_type(self, document_type, force=False):
        has_changed = self.document_type != document_type

        self.document_type = document_type
        self.save()
        if has_changed or force:
            post_document_type_change.send(sender=self.__class__, instance=self)

    @staticmethod
    def clear_image_cache():
        for the_file in os.listdir(CACHE_PATH):
            file_path = os.path.join(CACHE_PATH, the_file)
            if os.path.isfile(file_path):
                os.unlink(file_path)

    def __unicode__(self):
        return self.label

    @models.permalink
    def get_absolute_url(self):
        return ('documents:document_view_simple', [self.pk])

    def save(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        new_document = not self.pk
        super(Document, self).save(*args, **kwargs)

        if new_document:
            apply_default_acls(self, user)

            if user:
                self.add_as_recent_document_for_user(user)
                create_history(HISTORY_DOCUMENT_CREATED, self, {'user': user})
            else:
                create_history(HISTORY_DOCUMENT_CREATED, self)

    def get_cached_image_name(self, page, version):
        document_version = DocumentVersion.objects.get(pk=version)
        document_page = document_version.pages.get(page_number=page)
        transformations, warnings = document_page.get_transformation_list()
        hash_value = HASH_FUNCTION(u''.join([document_version.checksum, unicode(page), unicode(transformations)]))
        return os.path.join(CACHE_PATH, hash_value), transformations

    def get_image_cache_name(self, page, version):
        cache_file_path, transformations = self.get_cached_image_name(page, version)
        if os.path.exists(cache_file_path):
            return cache_file_path
        else:
            document_version = DocumentVersion.objects.get(pk=version)
            document_file = document_version.document.document_save_to_temp_dir(document_version.checksum)
            return convert(input_filepath=document_file, output_filepath=cache_file_path, page=page, transformations=transformations, mimetype=self.file_mimetype)

    def get_valid_image(self, size=DISPLAY_SIZE, page=DEFAULT_PAGE_NUMBER, zoom=DEFAULT_ZOOM_LEVEL, rotation=DEFAULT_ROTATION, version=None):
        if not version:
            version = self.latest_version.pk
        image_cache_name = self.get_image_cache_name(page=page, version=version)

        logger.debug('image_cache_name: %s' % image_cache_name)

        return convert(input_filepath=image_cache_name, cleanup_files=False, size=size, zoom=zoom, rotation=rotation)

    def get_image(self, size=DISPLAY_SIZE, page=DEFAULT_PAGE_NUMBER, zoom=DEFAULT_ZOOM_LEVEL, rotation=DEFAULT_ROTATION, as_base64=False, version=None):
        if zoom < ZOOM_MIN_LEVEL:
            zoom = ZOOM_MIN_LEVEL

        if zoom > ZOOM_MAX_LEVEL:
            zoom = ZOOM_MAX_LEVEL

        rotation = rotation % 360

        file_path = self.get_valid_image(size=size, page=page, zoom=zoom, rotation=rotation, version=version)
        logger.debug('file_path: %s' % file_path)

        if as_base64:
            mimetype = get_mimetype(open(file_path, 'r'), file_path, mimetype_only=True)[0]
            image = open(file_path, 'r')
            base64_data = base64.b64encode(image.read())
            image.close()
            return u'data:%s;base64,%s' % (mimetype, base64_data)
        else:
            return file_path

    def invalidate_cached_image(self, page):
        try:
            os.unlink(self.get_cached_image_name(page, self.latest_version.pk)[0])
        except OSError:
            pass

    def add_as_recent_document_for_user(self, user):
        RecentDocument.objects.add_document_for_user(user, self)

    def delete(self, *args, **kwargs):
        for version in self.versions.all():
            version.delete()
        return super(Document, self).delete(*args, **kwargs)

    @property
    def size(self):
        return self.latest_version.size

    def new_version(self, file_object, user=None, comment=None, version_update=None):
        logger.debug('creating new document version')
        # TODO: move this restriction to a signal processor of the checkouts app
        if not self.is_new_versions_allowed(user=user):
            raise NewDocumentVersionNotAllowed

        if version_update:
            # TODO: remove get_new_version_dict and just past the major, minor
            # and micro values
            new_version_dict = self.latest_version.get_new_version_dict(version_update)
        else:
            new_version_dict = {}

        logger.debug('new_version_dict: %s' % new_version_dict)

        new_version = DocumentVersion(
            document=self,
            file=file_object,
            major=new_version_dict.get('major') or 1,
            minor=new_version_dict.get('minor') or 0,
            micro=new_version_dict.get('micro') or 0,
            comment=comment or '',
        )
        new_version.save()

        logger.debug('new_version saved')

        # TODO: new HISTORY for version updates

        return new_version

    # Proxy methods
    def open(self, *args, **kwargs):
        """
        Return a file descriptor to a document's file irrespective of
        the storage backend
        """
        return self.latest_version.open(*args, **kwargs)

    def save_to_file(self, *args, **kwargs):
        return self.latest_version.save_to_file(*args, **kwargs)

    def exists(self):
        """
        Returns a boolean value that indicates if the document's
        latest version file exists in storage
        """
        return self.latest_version.exists()

    # Compatibility methods
    @property
    def file(self):
        return self.latest_version.file

    @property
    def file_mimetype(self):
        return self.latest_version.mimetype

    # TODO: rename to file_encoding
    @property
    def file_mime_encoding(self):
        return self.latest_version.encoding

    @property
    def date_updated(self):
        return self.latest_version.timestamp

    @property
    def checksum(self):
        return self.latest_version.checksum

    @property
    def signature_state(self):
        return self.latest_version.signature_state

    @property
    def pages(self):
        try:
            return self.latest_version.pages
        except AttributeError:
            # Document has no version yet
            return 0

    @property
    def page_count(self):
        return self.latest_version.page_count

    @property
    def latest_version(self):
        return self.versions.order_by('timestamp').last()

    def document_save_to_temp_dir(self, filename, buffer_size=1024 * 1024):
        temporary_path = os.path.join(TEMPORARY_DIRECTORY, filename)
        return self.save_to_file(temporary_path, buffer_size)


class DocumentVersion(models.Model):
    """
    Model that describes a document version and its properties
    """
    _pre_open_hooks = {}
    _post_save_hooks = {}

    @staticmethod
    def get_version_update_choices(document_version):
        return (
            (VERSION_UPDATE_MAJOR, _(u'Major %(major)i.%(minor)i.%(micro)i, (new release)') % document_version.get_new_version_dict(VERSION_UPDATE_MAJOR)),
            (VERSION_UPDATE_MINOR, _(u'Minor %(major)i.%(minor)i.%(micro)i, (some updates)') % document_version.get_new_version_dict(VERSION_UPDATE_MINOR)),
            (VERSION_UPDATE_MICRO, _(u'Micro %(major)i.%(minor)i.%(micro)i, (fixes)') % document_version.get_new_version_dict(VERSION_UPDATE_MICRO))
        )

    @classmethod
    def register_pre_open_hook(cls, order, func):
        cls._pre_open_hooks[order] = func

    @classmethod
    def register_post_save_hook(cls, order, func):
        cls._post_save_hooks[order] = func

    document = models.ForeignKey(Document, verbose_name=_(u'Document'), related_name='versions')
    major = models.PositiveIntegerField(verbose_name=_(u'Mayor'), default=1)
    minor = models.PositiveIntegerField(verbose_name=_(u'Minor'), default=0)
    micro = models.PositiveIntegerField(verbose_name=_(u'Micro'), default=0)
    timestamp = models.DateTimeField(verbose_name=_(u'Timestamp'), auto_now_add=True)
    comment = models.TextField(blank=True, verbose_name=_(u'Comment'))

    # File related fields
    file = models.FileField(upload_to=lambda: UUID_FUNCTION(), storage=storage_backend, verbose_name=_(u'File'))
    mimetype = models.CharField(max_length=255, null=True, blank=True, editable=False)
    encoding = models.CharField(max_length=64, null=True, blank=True, editable=False)

    checksum = models.TextField(blank=True, null=True, verbose_name=_(u'Checksum'), editable=False)

    class Meta:
        unique_together = ('document', 'major', 'minor', 'micro')
        verbose_name = _(u'Document version')
        verbose_name_plural = _(u'Document version')

    def __unicode__(self):
        return self.get_formated_version()

    def get_new_version_dict(self, version_update_type):
        logger.debug('version_update_type: %s' % version_update_type)

        if version_update_type == VERSION_UPDATE_MAJOR:
            return {
                'major': self.major + 1,
                'minor': 0,
                'micro': 0,
            }
        elif version_update_type == VERSION_UPDATE_MINOR:
            return {
                'major': self.major,
                'minor': self.minor + 1,
                'micro': 0,
            }
        elif version_update_type == VERSION_UPDATE_MICRO:
            return {
                'major': self.major,
                'minor': self.minor,
                'micro': self.micro + 1,
            }

    def get_formated_version(self):
        """
        Return the formatted version information
        """
        return u'%i.%i.%i' % (self.major, self.minor, self.micro)

    def save(self, *args, **kwargs):
        """
        Overloaded save method that updates the document version's checksum,
        mimetype, page count and transformation when created
        """
        new_document_version = not self.pk

        # Only do this for new documents
        transformations = kwargs.pop('transformations', None)
        super(DocumentVersion, self).save(*args, **kwargs)

        for key in sorted(DocumentVersion._post_save_hooks):
            DocumentVersion._post_save_hooks[key](self)

        if new_document_version:
            # Only do this for new documents
            self.update_checksum(save=False)
            self.update_mimetype(save=False)
            self.save()
            self.update_page_count(save=False)

            if transformations:
                self.apply_default_transformations(transformations)

            post_version_upload.send(sender=self.__class__, instance=self)

    def update_checksum(self, save=True):
        """
        Open a document version's file and update the checksum field using the
        user provided checksum function
        """
        if self.exists():
            source = self.open()
            self.checksum = unicode(CHECKSUM_FUNCTION(source.read()))
            source.close()
            if save:
                self.save()

    def update_page_count(self, save=True):
        handle, filepath = tempfile.mkstemp()
        # Just need the filepath, close the file description
        os.close(handle)

        self.save_to_file(filepath)
        try:
            detected_pages = get_page_count(filepath)
        except UnknownFileFormat:
            # If converter backend doesn't understand the format,
            # use 1 as the total page count
            detected_pages = 1
            self.description = ugettext(u'This document\'s file format is not known, the page count has therefore defaulted to 1.')
            self.save()
        try:
            os.remove(filepath)
        except OSError:
            pass

        current_pages = self.pages.order_by('page_number',)
        if current_pages.count() > detected_pages:
            for page in current_pages[detected_pages:]:
                page.delete()

        for page_number in range(detected_pages):
            DocumentPage.objects.get_or_create(
                document_version=self, page_number=page_number + 1)

        if save:
            self.save()

        return detected_pages

    # TODO: remove from here and move to converter app
    def apply_default_transformations(self, transformations):
        # Only apply default transformations on new documents
        if reduce(lambda x, y: x + y, [page.documentpagetransformation_set.count() for page in self.pages.all()]) == 0:
            for transformation in transformations:
                for document_page in self.pages.all():
                    page_transformation = DocumentPageTransformation(
                        document_page=document_page,
                        order=0,
                        transformation=transformation.get('transformation'),
                        arguments=transformation.get('arguments')
                    )

                    page_transformation.save()

    def revert(self):
        """
        Delete the subsequent versions after this one
        """
        for version in self.document.versions.filter(timestamp__gt=self.timestamp):
            version.delete()

    def update_mimetype(self, save=True):
        """
        Read a document verions's file and determine the mimetype by calling the
        get_mimetype wrapper
        """
        if self.exists():
            try:
                self.mimetype, self.encoding = get_mimetype(self.open(), self.document.label)
            except:
                self.mimetype = u''
                self.encoding = u''
            finally:
                if save:
                    self.save()

    def delete(self, *args, **kwargs):
        self.file.storage.delete(self.file.path)
        return super(DocumentVersion, self).delete(*args, **kwargs)

    def exists(self):
        """
        Returns a boolean value that indicates if the document's file
        exists in storage
        """
        return self.file.storage.exists(self.file.path)

    def open(self, raw=False):
        """
        Return a file descriptor to a document version's file irrespective of
        the storage backend
        """
        if raw:
            return self.file.storage.open(self.file.path)
        else:
            result = self.file.storage.open(self.file.path)
            for key in sorted(DocumentVersion._pre_open_hooks):
                result = DocumentVersion._pre_open_hooks[key](result, self)

            return result

    def save_to_file(self, filepath, buffer_size=1024 * 1024):
        """
        Save a copy of the document from the document storage backend
        to the local filesystem
        """
        input_descriptor = self.open()
        output_descriptor = open(filepath, 'wb')
        while True:
            copy_buffer = input_descriptor.read(buffer_size)
            if copy_buffer:
                output_descriptor.write(copy_buffer)
            else:
                break

        output_descriptor.close()
        input_descriptor.close()
        return filepath

    @property
    def size(self):
        if self.exists():
            return self.file.storage.size(self.file.path)
        else:
            return None

    # TODO: Remove
    def rename(self, new_name):
        new_filename, new_extension = os.path.splitext(new_name)
        name, extension = os.path.splitext(self.filename)

        # Preserve existing extension if new name doesn't has one
        if new_extension:
            extension = new_extension

        self.filename = u''.join([new_filename, extension])
        self.save()

    @property
    def page_count(self):
        return self.pages.count()


class DocumentTypeFilename(models.Model):
    """
    List of filenames available to a specific document type for the
    quick rename functionality
    """
    document_type = models.ForeignKey(DocumentType, related_name='filenames', verbose_name=_(u'Document type'))
    filename = models.CharField(max_length=128, verbose_name=_(u'Filename'), db_index=True)
    enabled = models.BooleanField(default=True, verbose_name=_(u'Enabled'))

    def __unicode__(self):
        return self.filename

    class Meta:
        ordering = ['filename']
        verbose_name = _(u'Document type quick rename filename')
        verbose_name_plural = _(u'Document types quick rename filenames')


class DocumentPage(models.Model):
    """
    Model that describes a document version page including it's content
    """
    document_version = models.ForeignKey(DocumentVersion, verbose_name=_(u'Document version'), related_name='pages')
    content = models.TextField(blank=True, null=True, verbose_name=_(u'Content'))
    page_label = models.CharField(max_length=40, blank=True, null=True, verbose_name=_(u'Page label'))
    page_number = models.PositiveIntegerField(default=1, editable=False, verbose_name=_(u'Page number'), db_index=True)

    def __unicode__(self):
        return _(u'Page %(page_num)d out of %(total_pages)d of %(document)s') % {
            'document': unicode(self.document),
            'page_num': self.page_number,
            'total_pages': self.document_version.pages.count()
        }

    class Meta:
        ordering = ['page_number']
        verbose_name = _(u'Document page')
        verbose_name_plural = _(u'Document pages')

    @models.permalink
    def get_absolute_url(self):
        return ('documents:document_page_view', [self.pk])

    @property
    def siblings(self):
        return DocumentPage.objects.filter(document_version=self.document_version)

    # Compatibility methods
    @property
    def document(self):
        return self.document_version.document


class ArgumentsValidator(object):
    message = _(u'Enter a valid value.')
    code = 'invalid'

    def __init__(self, message=None, code=None):
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code

    def __call__(self, value):
        """
        Validates that the input evaluates correctly.
        """
        value = value.strip()
        try:
            literal_eval(value)
        except (ValueError, SyntaxError):
            raise ValidationError(self.message, code=self.code)


class DocumentPageTransformation(models.Model):
    """
    Model that stores the transformation and transformation arguments
    for a given document page
    """
    document_page = models.ForeignKey(DocumentPage, verbose_name=_(u'Document page'))
    order = models.PositiveIntegerField(default=0, blank=True, null=True, verbose_name=_(u'Order'), db_index=True)
    transformation = models.CharField(choices=get_available_transformations_choices(), max_length=128, verbose_name=_(u'Transformation'))
    arguments = models.TextField(blank=True, null=True, verbose_name=_(u'Arguments'), help_text=_(u'Use dictionaries to indentify arguments, example: {\'degrees\':90}'), validators=[ArgumentsValidator()])
    objects = DocumentPageTransformationManager()

    def __unicode__(self):
        return self.get_transformation_display()

    class Meta:
        ordering = ('order',)
        verbose_name = _(u'Document page transformation')
        verbose_name_plural = _(u'Document page transformations')


class RecentDocument(models.Model):
    """
    Keeps a list of the n most recent accessed or created document for
    a given user
    """
    user = models.ForeignKey(User, verbose_name=_(u'User'), editable=False)
    document = models.ForeignKey(Document, verbose_name=_(u'Document'), editable=False)
    datetime_accessed = models.DateTimeField(verbose_name=_(u'Accessed'), auto_now=True, db_index=True)

    objects = RecentDocumentManager()

    def __unicode__(self):
        return unicode(self.document)

    class Meta:
        ordering = ('-datetime_accessed',)
        verbose_name = _(u'Recent document')
        verbose_name_plural = _(u'Recent documents')


# Quick hack to break the DocumentPage and DocumentPageTransformation circular dependency
# Can be remove once the transformations are moved to the converter app
DocumentPage.add_to_class('get_transformation_list', lambda document_page: DocumentPageTransformation.objects.get_for_document_page_as_list(document_page))
