import numpy as np
import pandas as pd

from citation.models import Publication, Tag, Sponsor, Platform, Author, Container


def container_as_dict(c: Container):
    return {'id': c.id, 'name': c.name}


def author_as_dict(a: Author):
    return {'id': a.id, 'name': a.name}


def publication_as_dict(p: Publication):
    return {'id': p.id,
            'author_ids': [a.id for a in p.creators.all()],
            'container': container_as_dict(p.container),
            'date_published': p.date_published,
            'year_published': p.date_published.year if p.date_published is not None else None,
            'flagged': p.flagged,
            'is_archived': p.is_archived,
            'model_documentation': [m.name for m in p.model_documentation.all()],
            'status': p.status,
            'tags': [t.name for t in p.tags.all()],
            'title': p.title}


class AbstractCacheModel:
    pk_field_name = 'pk'
    option_label_field_name = 'name'
    option_value_field_name = 'id'
    prefetches = None
    related_name = 'publications'

    def get_queryset(self):
        raise NotImplemented()

    def get_bulk_queryset(self):
        return self.get_queryset().prefetch_related(*self.prefetches).in_bulk(field_name=self.pk_field_name)

    @property
    def objs(self):
        if not hasattr(self, '_objs'):
            self._objs = self.get_queryset()
        return self._objs

    @property
    def publication_lookup(self):
        if not hasattr(self, '_pk_lookup'):
            bulk = self.get_bulk_queryset()
            self._pk_lookup = {}
            for k in bulk.keys():
                self._pk_lookup[k] = {'obj': bulk[k], 'ids': list(getattr(bulk[k], self.related_name).values_list('id', flat=True))}
        return self._pk_lookup

    @property
    def options(self):
        if not hasattr(self, '_options'):
            self._options = [{'label': getattr(o, self.option_label_field_name),
                              'value': getattr(o, self.option_value_field_name)} for o in self.objs]
        return self._options


class AuthorCache(AbstractCacheModel):
    prefetches = ['publication_authors', 'publication_authors__author']

    def get_queryset(self):
        return Author.objects.filter(id__in=Publication.api.primary().values_list('creators__id', flat=True)) \
            .order_by('family_name', 'given_name')


class PlatformCache(AbstractCacheModel):
    pk_field_name = 'name'
    option_value_field_name = 'name'
    prefetches = ['publication_platforms', 'publication_platforms__platform']

    def get_queryset(self):
        return Platform.objects.filter(id__in=Publication.api.primary().values_list('platforms__id', flat=True)) \
            .order_by('name')


class SponsorCache(AbstractCacheModel):
    pk_field_name = 'name'
    option_value_field_name = 'name'
    prefetches = ['publication_sponsors', 'publication_sponsors__sponsor']

    def get_queryset(self):
        return Sponsor.objects.filter(id__in=Publication.api.primary().values_list('sponsors__id', flat=True)) \
            .order_by('name')


class TagCache(AbstractCacheModel):
    pk_field_name = 'name'
    option_value_field_name = 'name'
    prefetches = ['publication_tags', 'publication_tags__tag']
    related_name = 'publication_set'

    def get_queryset(self):
        return Tag.objects.filter(id__in=Publication.api.primary().values_list('tags__id', flat=True)).order_by('name')


class DataCache:
    def __init__(self):
        self.authors = AuthorCache()
        self.platforms = PlatformCache()
        self.sponsors = SponsorCache()
        self.tags = TagCache()

    @staticmethod
    def _get_publication_df():
        df = pd.DataFrame.from_records([publication_as_dict(p) for p in
                                        Publication.api.prefetch_related(
                                            'publication_authors', 'publication_authors__author',
                                            'model_documentation', 'publication_tags', 'publication_tags__tag')
                                       .select_related('container').primary()], index='id')
        return df

    @property
    def publication_df(self):
        if not hasattr(self, '_publication_df'):
            self._publication_df = self._get_publication_df()
        return self._publication_df


class PublicationQueries:
    def __init__(self, df: pd.DataFrame):
        self.df = df

    # def build_indices(self):
    #     for author_ids in self.df.author_ids:

    def filter_by_date_published(self, start_year: int, end_year: int):
        return PublicationQueries(self.df[(start_year < self.df.year_published) & (self.df.year_published <= end_year)])

    def filter_by_author_pk(self, pk):
        return PublicationQueries(self.df[self.df.author_ids.apply(lambda pks: pk in pks)])

    def filter_by_platform_pk(self, pk):
        return PublicationQueries(self.df[self.df.platform_pks.apply(lambda pks: pk in pks)])

    def filter_by_tag(self, tag: str):
        return PublicationQueries(self.df.query('@tag in tags'))

    def filter_by_pk(self, model_name, pk):
        if model_name == Author._meta.verbose_name:
            return self.filter_by_author_pk(pk)
        elif model_name == Platform._meta.verbose_name:
            return

    def to_is_archived(self):
        df = self.df.groupby('year_published')[['year_published', 'is_archived']].aggregate(
            dict(is_archived=np.sum, year_published='count'))
        df.set_index('year_published')
        return df
