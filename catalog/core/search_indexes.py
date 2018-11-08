import logging
from urllib.parse import urlencode

from django.db.models import QuerySet
from django.urls import reverse
from haystack import indexes
from typing import Dict, List

from citation.models import Publication, Platform, Sponsor, Tag, ModelDocumentation, Container, Author

logger = logging.getLogger(__name__)


##########################################
#  Publication query seach/filter index  #
##########################################

class PublicationIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, use_template=True)
    title = indexes.CharField(model_attr='title')
    date_published = indexes.DateField(model_attr='date_published', null=True)
    last_modified = indexes.DateTimeField(model_attr='date_modified')
    contact_email = indexes.BooleanField(model_attr='contact_email')
    status = indexes.CharField(model_attr='status', faceted=True)
    container = indexes.CharField(model_attr='container__name', null=True)
    tags = indexes.EdgeNgramField(model_attr='tags__name', null=True)
    sponsors = indexes.CharField(model_attr='sponsors__name', null=True)
    platforms = indexes.CharField(model_attr='platforms__name', null=True)
    model_documentation = indexes.CharField(model_attr='model_documentation__name', null=True)
    authors = indexes.CharField(model_attr='creators__name', null=True)
    assigned_curator = indexes.CharField(model_attr='assigned_curator', null=True)
    flagged = indexes.BooleanField(model_attr='flagged')
    is_primary = indexes.BooleanField(model_attr='is_primary')
    is_archived = indexes.BooleanField(model_attr='is_archived')
    contributor_data = indexes.MultiValueField(model_attr='contributor_data', null=True)

    def prepare_last_modified(self, obj):
        last_modified = self.prepared_data.get('last_modified')
        if last_modified:
            return last_modified.strftime('%Y-%m-%dT%H:%M:%SZ')
        return ''

    def prepare_contributor_data(self, obj):
        contributor_data = self.prepared_data.get('contributor_data')
        if contributor_data:
            return '{0} ({1})%'.format(contributor_data[0]['creator'], contributor_data[0]['contribution'])
        return ''

    def get_model(self):
        return Publication

    def index_queryset(self, using=None):
        return Publication.objects.filter(is_primary=True)


##########################################
#       AutoComplete Index Fields        #
##########################################

class NameAutocompleteIndex(indexes.SearchIndex):
    text = indexes.CharField(document=True)
    name = indexes.NgramField(model_attr='name')

    class Meta:
        abstract = True


class PlatformIndex(NameAutocompleteIndex, indexes.Indexable):
    def get_model(self):
        return Platform


class SponsorIndex(NameAutocompleteIndex, indexes.Indexable):
    def get_model(self):
        return Sponsor


class TagIndex(NameAutocompleteIndex, indexes.Indexable):
    def get_model(self):
        return Tag


class ModelDocumentationIndex(NameAutocompleteIndex, indexes.Indexable):
    def get_model(self):
        return ModelDocumentation


##########################################
#           Bulk Index Updates           #
##########################################

def bulk_index_update():
    PublicationIndex().update()
    PlatformIndex().update()
    SponsorIndex().update()
    TagIndex().update()
    ModelDocumentationIndex().update()


##########################################
#           Public Indices               #
##########################################

from elasticsearch.helpers import bulk
from elasticsearch_dsl import DocType, connections, InnerDoc, aggs, query
import elasticsearch_dsl as edsl

ALL_DATA_FIELD = 'all_data'


class AuthorInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    orcid = edsl.Keyword()
    researcherid = edsl.Keyword()
    email = edsl.Keyword()
    name = edsl.Text(copy_to=ALL_DATA_FIELD)


class ContainerInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD)
    issn = edsl.Keyword()


class RelatedInnerDoc(InnerDoc):
    id = edsl.Integer(required=True)
    name = edsl.Text(copy_to=ALL_DATA_FIELD)


class TopHits:
    def __init__(self, iterable, hits):
        self.iterable = iterable
        self.hits = hits

    def __iter__(self):
        return iter(self.iterable)


class AbstractAgg:
    def __init__(self, name):
        self.name = name

    def extract(self, response, ids):
        data = self.extract_count(response, ids)
        return {self.name: {'count': data}}


# Use top hits elasticsearch aggregator to avoid hitting DB
class UnnestedAgg(AbstractAgg):
    @property
    def _terms_bucket_name(self):
        return 'top_{}_count'.format(self.name)

    _top_hit_bucket_name = 'top_hit'

    def count(self, search):
        search.aggs.bucket(self._terms_bucket_name,
                           aggs.Terms(field='{}.id'.format(self.name))) \
            .bucket(self._top_hit_bucket_name,
                    aggs.TopHits(size=1, _source={'includes': [self.name]}))

    def extract_count(self, response, ids):
        term_buckets = response.aggs[self._terms_bucket_name].buckets
        results = []
        for bucket in term_buckets:
            result = {'publication_count': bucket.doc_count}
            result.update(bucket[self._top_hit_bucket_name].hits.hits[0]['_source'][self.name])
            result['checked'] = result['id'] in ids
            results.append(result)
        return results


class NestedAgg(AbstractAgg):
    @property
    def _top_bucket_name(self):
        return '{}'.format(self.name)

    _terms_bucket_name = 'top_count'
    _top_hit_bucket_name = 'top_hit'

    def count(self, search):
        search.aggs.bucket(self._top_bucket_name, aggs.Nested(path=self.name)) \
            .bucket(self._terms_bucket_name,
                    aggs.Terms(field='{}.id'.format(self.name))) \
            .bucket(self._top_hit_bucket_name, aggs.TopHits(size=1, _source={'includes': [self.name]}))

    def extract_count(self, response, ids):
        term_buckets = response.aggs[self._top_bucket_name][self._terms_bucket_name].buckets
        results = []
        for bucket in term_buckets:
            result = {'publication_count': bucket.doc_count}
            result.update(bucket[self._top_hit_bucket_name].hits.hits[0]['_source'])
            result['checked'] = result['id'] in ids
            logger.info('checked: %s, %i, %s', result['checked'], result['id'], ids)
            results.append(result)
        return results


class FilterQuery:
    def __init__(self, name):
        self.field = '{}.id'.format(name)

    def by_ids(self, ids):
        return query.Q('terms', **{self.field: list(ids)})


class NestedFilterQuery:
    def __init__(self, name):
        self.path = name
        self.field = '{}.id'.format(name)

    def by_ids(self, ids):
        return query.Nested(path=self.path, query=query.Q('terms', **{self.field: list(ids)}))


class PublicationDocSearch:
    AUTHOR_FIELD_NAME = 'authors'
    CONTAINER_FIELD_NAME = 'container'
    PLATFORM_FIELD_NAME = 'platforms'
    SPONSOR_FIELD_NAME = 'sponsors'
    TAG_FIELD_NAME = 'tags'

    aggs = {
        AUTHOR_FIELD_NAME: NestedAgg(AUTHOR_FIELD_NAME),
        CONTAINER_FIELD_NAME: UnnestedAgg(CONTAINER_FIELD_NAME),
        PLATFORM_FIELD_NAME: NestedAgg(PLATFORM_FIELD_NAME),
        SPONSOR_FIELD_NAME: NestedAgg(SPONSOR_FIELD_NAME),
        TAG_FIELD_NAME: NestedAgg(TAG_FIELD_NAME)
    }

    filters = {
        AUTHOR_FIELD_NAME: NestedFilterQuery(AUTHOR_FIELD_NAME),
        CONTAINER_FIELD_NAME: FilterQuery(CONTAINER_FIELD_NAME),
        PLATFORM_FIELD_NAME: NestedFilterQuery(PLATFORM_FIELD_NAME),
        SPONSOR_FIELD_NAME: NestedFilterQuery(SPONSOR_FIELD_NAME),
        TAG_FIELD_NAME: NestedFilterQuery(TAG_FIELD_NAME)
    }

    def __init__(self, search=None, cache=None):
        self.search = PublicationDoc.search() if search is None else search
        self.cache = {} if cache is None else cache

    def __getitem__(self, val):
        return PublicationDocSearch(self.search[val])

    def _full_text(self, q):
        return query.Match(**{ALL_DATA_FIELD: q})

    def _filter(self, field_name_to_ids: Dict[str, List[int]]):
        queries = []
        for field_name in field_name_to_ids:
            ids = field_name_to_ids[field_name]
            queries.append(self.filters[field_name].by_ids(ids))
        return queries

    def find(self, q, field_name_to_ids):
        queries = self._filter(field_name_to_ids)
        full_text = self._full_text(q) if q else query.MatchAll()
        if queries:
            return PublicationDocSearch(self.search.query(query.Bool(should=queries, must=[full_text])))
        elif q:
            return PublicationDocSearch(self.search.query(full_text))
        else:
            return PublicationDocSearch(self.search.sort('-date_published'))

    def source(self, fields=None, **kwargs):
        return PublicationDocSearch(self.search.source(fields=fields, **kwargs))

    def scan(self):
        return self.search.scan()

    def agg_by_count(self):
        s = self.search._clone()
        for agg in self.aggs.values():
            agg.count(s)
        return PublicationDocSearch(s)

    @classmethod
    def get_filter_field_names(cls):
        return [cls.AUTHOR_FIELD_NAME, cls.CONTAINER_FIELD_NAME,
                cls.PLATFORM_FIELD_NAME, cls.SPONSOR_FIELD_NAME, cls.TAG_FIELD_NAME]

    def execute(self, filters):
        response = self.search.execute()
        for name in self.aggs:
            ids = filters.get(name, [])
            agg = self.aggs[name]
            self.cache.update(agg.extract(response, ids))
        return response


class PublicationDoc(DocType):
    all_data = edsl.Text()
    id = edsl.Integer()
    title = edsl.Text(copy_to=ALL_DATA_FIELD)
    date_published = edsl.Date()
    last_modified = edsl.Date()
    contact_email = edsl.Keyword(copy_to=ALL_DATA_FIELD)
    container = edsl.Object(ContainerInnerDoc)
    tags = edsl.Nested(RelatedInnerDoc)
    sponsors = edsl.Nested(RelatedInnerDoc)
    platforms = edsl.Nested(RelatedInnerDoc)
    model_documentation = edsl.Keyword()
    authors = edsl.Nested(AuthorInnerDoc)

    @classmethod
    def from_instance(cls, publication):
        container = publication.container
        doc = cls(meta={'id': publication.id},
                  id=publication.id,
                  title=publication.title,
                  date_published=publication.date_published,
                  last_modified=publication.date_modified,
                  contact_email=publication.contact_email,
                  container=ContainerInnerDoc(id=container.id, name=container.name, issn=container.issn),
                  tags=[RelatedInnerDoc(id=t.id, name=t.name) for t in publication.tags.all()],
                  sponsors=[RelatedInnerDoc(id=s.id, name=s.name) for s in publication.sponsors.all()],
                  platforms=[RelatedInnerDoc(id=p.id, name=p.name) for p in publication.platforms.all()],
                  model_documentation=[md.name for md in publication.model_documentation.all()],
                  authors=[
                      AuthorInnerDoc(id=a.id, name=a.name, orcid=a.orcid, researcherid=a.researcherid, email=a.email)
                      for a in publication.creators.all()])
        return doc.to_dict(include_meta=True)

    def get_public_detail_url(self):
        return reverse('core:public-publication-detail', kwargs={'pk': self.meta.id})

    @classmethod
    def get_breadcrumb_data(cls):
        return {'breadcrumb_trail': [
            {'link': reverse('core:public-home'), 'text': 'Home'},
            {'text': 'Publications'}
        ]}

    @classmethod
    def get_public_list_url(cls, q=None):
        location = reverse('core:public-search')
        if q:
            query_string = urlencode({'q': q})
            location += '?{}'.format(query_string)
        return location

    class Index:
        name = 'publication'


def bulk_index_public():
    public_publications = Publication.api.primary().filter(status='REVIEWED')
    PublicationDoc.init()
    logger.info('creating publication index')
    bulk(client=connections.get_connection(),
         actions=(PublicationDoc.from_instance(p) for p in public_publications.select_related('container') \
         .prefetch_related('tags', 'sponsors', 'platforms', 'creators', 'model_documentation').iterator()))
