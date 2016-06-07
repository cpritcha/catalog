from django.test import TestCase
from catalog.citation.ingest import load_bibtex, ingest
from ..bibtex import ref as bibtex_ref_api, entry as bibtex_entry_api
from ..crossref import common
from .. import merger, models, util
from typing import List

import copy

import ast


class TestAuthorStr(TestCase):
    def test_author_str_and_seperated(self):
        author_str = 'Murray-Rust, D. and Brown, C. and van Vliet, J. and Alam, S. J. and\nRobinson, D. T. and Verburg, P. H. and Rounsevell, M.'
        author_split = bibtex_entry_api.guess_author_str_split(author_str)
        self.assertEqual(author_split[1:3], ["Brown, C.", "van Vliet, J."])


class TestCitationParsing(TestCase):
    def test_wifi_tracking_solu(self):
        ref = "1 WIFI TRACKING SOLU."
        author_str, year_str, container_str = bibtex_ref_api.guess_elements(ref)
        self.assertEqual(author_str, ref)

    def test_pappalardo(self):
        ref = "1 Pappalardo F., 2011, BMC CANC UNPUB."
        author_str, year_str, container_str = bibtex_ref_api.guess_elements(ref)
        self.assertEqual(author_str, "1 Pappalardo F.")
        self.assertEqual(year_str, "2011")
        self.assertEqual(container_str, "BMC CANC UNPUB.")

    def test_intelligent_agents(self):
        ref = "2002, INTELLIGENT AGENTS C, V10, P325."
        author_str, year_str, container_str = bibtex_ref_api.guess_elements(ref)
        self.assertEqual(author_str, "")
        self.assertEqual(year_str, "2002")
        self.assertEqual(container_str, "INTELLIGENT AGENTS C")


class TestPublication(TestCase):
    @classmethod
    def setUpClass(cls):
        super(TestPublication, cls).setUpClass()
        with open("catalog/citation/tests/data/problematic_entries", "r") as f:
            contents = f.readlines()

        cls.walderr2013 = ast.literal_eval(contents[0], )
        cls.galente2012 = ast.literal_eval(contents[1])


class TestEntryParsing(TestPublication):
    def test_walderr2013(self):
        publication = bibtex_entry_api.process(self.walderr2013)
        self.assertEqual([a.name for a in publication.raw_authors.all()],
                         ["WALDHERR ANNIE", "WIJERMANS NANDA"])


class TestNameNormalization(TestCase):
    def test_normalize_name(self):
        abbas_plain = "Abbas AK"
        abbas_period = "Abbas A. K."
        abbas_caps = "ABBAS AK"
        abbas_caps_no_middle = "ABBAS A"
        abbas_comma = "Abbas, AK"
        abbas_full = "Abbas, Alfred K"

        normalized_name = util.normalize_name(abbas_plain)
        self.assertEqual(normalized_name, util.normalize_name(abbas_caps))
        self.assertEqual(normalized_name, util.normalize_name(abbas_comma))

        last_name_and_initials_str = util.last_name_and_initials(normalized_name)
        self.assertEqual(last_name_and_initials_str,
                         util.last_name_and_initials(
                             util.normalize_name(abbas_period)
                         ))
        self.assertNotEqual(last_name_and_initials_str,
                            util.last_name_and_initials(
                                util.normalize_name(abbas_caps_no_middle)
                            ))
        self.assertEqual(last_name_and_initials_str,
                         util.last_name_and_initials(
                             util.normalize_name(abbas_full)
                         ))

        last_name_and_initial_str = util.last_name_and_initial(last_name_and_initials_str)
        self.assertEqual(last_name_and_initial_str, "ABBAS A")


class TestAuthorAliasMerging(TestCase):
    def setUp(self):
        self.raw_bibtex = models.Raw.objects.create(key=models.Raw.BIBTEX_ENTRY, value="")
        self.raw_crossref = models.Raw.objects.create(key=models.Raw.CROSSREF_DOI_SUCCESS, value="")

        self.pritchard = models.Author.objects.create(type=models.Author.INDIVIDUAL)
        self.pritchard_alias1 = models.AuthorAlias.objects.create(author=self.pritchard, name="PRITCHARD C")
        self.pritchard_alias2 = models.AuthorAlias.objects.create(author=self.pritchard, name="PRITCHARD CALVIN")

    def test_add_raw_already_in_db(self):
        raw_bibtex_pritchard = models.AuthorRaw.objects.create(
            name="PRITCHARD C",
            type=models.AuthorRaw.INDIVIDUAL,
            raw=self.raw_bibtex,
            publication_raw_id=self.raw_bibtex.id)
        grouped_authors = merger.group_authors(raw_authors=models.AuthorRaw.objects.all())
        matched_grouped_authors = merger.match_grouped_authors(grouped_authors)
        merger.add_grouped_authors(matched_grouped_authors)

        author = models.Author.objects.filter(authoralias__authorraw=raw_bibtex_pritchard).first() # type: models.Author
        self.assertIsNotNone(author)
        self.assertEqual(author.authoralias_set.count(), 2)
        self.assertEqual(self.pritchard_alias1, models.AuthorRaw.objects.first().author_alias)

    def test_add_raw_not_in_db(self):
        raw_foo = models.AuthorRaw.objects.create(
            name="Foo, Baz",
            type=models.AuthorRaw.INDIVIDUAL,
            raw=self.raw_bibtex,
            publication_raw_id=self.raw_bibtex.id
        )

        grouped_authors = merger.group_authors(raw_authors=models.AuthorRaw.objects.all())
        matched_grouped_authors = merger.match_grouped_authors(grouped_authors)
        merger.add_grouped_authors(matched_grouped_authors)

        author = models.Author.objects.filter(authoralias__authorraw=raw_foo).first()  # type: models.Author
        self.assertIsNotNone(author)
        self.assertEqual(author.authoralias_set.count(), 1)
        self.assertNotEqual(self.pritchard_alias1, models.AuthorRaw.objects.first().author_alias)
        self.assertNotEqual(self.pritchard_alias2, models.AuthorRaw.objects.first().author_alias)

    def test_group_authors_different(self):
        raw_bibtex_foo = models.AuthorRaw.objects.create(
            name="Foo, Baz",
            type=models.AuthorRaw.INDIVIDUAL,
            raw=self.raw_bibtex,
            publication_raw_id=self.raw_bibtex.id)
        raw_crossref_foo = models.AuthorRaw.objects.create(
            name="Foo, B.",
            type=models.AuthorRaw.INDIVIDUAL,
            raw=self.raw_crossref,
            publication_raw_id=self.raw_bibtex.id)
        grouped_authors = merger.group_authors(raw_authors=models.AuthorRaw.objects.all())
        self.assertEqual(len(grouped_authors), 1)
        self.assertListEqual(grouped_authors[0], [raw_bibtex_foo, raw_crossref_foo])

    def test_group_authors_same_raw_type(self):
        pass


class TestGroupAuthors(TestCase):
        def setUp(self):
            self.raw1 = models.Raw.objects.create(key=models.Raw.BIBTEX_ENTRY, value="")
            self.raw2 = models.Raw.objects.create(key=models.Raw.CROSSREF_DOI_SUCCESS, value="")
            self.foo_alias1 = models.AuthorRaw.objects.create(name="FOO B",
                                                              type=models.AuthorRaw.INDIVIDUAL,
                                                              raw=self.raw1,
                                                              publication_raw_id=self.raw1.id)
            self.foo_alias2 = models.AuthorRaw.objects.create(name="FOO BAR",
                                                              type=models.AuthorRaw.INDIVIDUAL,
                                                              raw=self.raw2,
                                                              publication_raw_id=self.raw2.id)
            self.bob_smith = models.AuthorRaw.objects.create(name="SMITH BOB",
                                                             type=models.AuthorRaw.INDIVIDUAL,
                                                             raw=self.raw2,
                                                             publication_raw_id=self.raw2.id)


class TestMergeSet(TestCase):

    def setUp(self):
        self.publication1 = models.Publication.objects.create(doi="10.1001/a", primary=True)
        self.publication2 = models.Publication.objects.create(doi="10.1001/a", primary=True)
        self.publication3 = models.Publication.objects.create(doi="10.1001/b", primary=True)

    def test_doi_merge(self):
        mergeset = merger.create_publication_mergeset_by_doi()
        self.assertEqual(len(mergeset._groups), 1)
        self.assertListEqual([self.publication1.id, self.publication2.id], mergeset._groups[0])