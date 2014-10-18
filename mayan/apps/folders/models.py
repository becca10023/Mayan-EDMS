from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import ugettext as _

from documents.models import Document


class Folder(models.Model):
    title = models.CharField(max_length=128, verbose_name=_(u'Title'), db_index=True)
    user = models.ForeignKey(User, verbose_name=_(u'User'))
    datetime_created = models.DateTimeField(verbose_name=_(u'Datetime created'), auto_now_add=True)
    documents = models.ManyToManyField(Document, verbose_name=_('Documents'))

    def __unicode__(self):
        return self.title

    @models.permalink
    def get_absolute_url(self):
        return ('folders:folder_view', [self.pk])

    class Meta:
        unique_together = ('title', 'user')
        ordering = ('title',)
        verbose_name = _(u'Folder')
        verbose_name_plural = _(u'Folders')
