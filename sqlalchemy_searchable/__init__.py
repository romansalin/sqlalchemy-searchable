import re
from pyparsing import ParseException

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.schema import DDL
from sqlalchemy_utils import TSVectorType
from validators import email
from .parser import SearchQueryParser, unicode_non_alnum


__version__ = '0.5.0'


parser = SearchQueryParser()


def filter_term(term):
    """
    Removes all illegal characters from the search term but only if given
    search term is not an email. PostgreSQL search vector parser notices email
    addresses hence we need special parsing for them here also.

    :param term: search term to filter
    """
    if email(term):
        return term
    else:
        return re.sub(r'[%s]+' % unicode_non_alnum, ' ', term)


def parse_search_query(query, parser=parser):
    query = query.strip()
    # Convert hyphens between words to spaces but leave all hyphens which are
    # at the beginning of the word (negation operator)
    query = re.sub(r'(?i)(?<=[^\s|^])-(?=[^\s])', ' ', query)

    parts = query.split()
    parts = [
        filter_term(part).strip() for part in parts if part
    ]
    query = ' '.join(parts)

    if not query:
        return u''

    try:
        return parser.parse(query)
    except ParseException:
        return u''


class SearchQueryMixin(object):
    def search(self, search_query, catalog=None):
        """
        Search given query with full text search.

        :param search_query: the search query
        """
        return search(self, search_query, catalog=catalog)


def inspect_search_vectors(entity):
    search_vectors = []
    for prop in entity.__mapper__.iterate_properties:
        if isinstance(prop, sa.orm.ColumnProperty):
            if isinstance(prop.columns[0].type, TSVectorType):
                search_vectors.append(getattr(entity, prop.key))
    return search_vectors


def search(query, search_query, vector=None, catalog=None):
    """
    Search given query with full text search.

    :param search_query: the search query
    :param vector: search vector to use
    :param catalog: postgresql catalog to be used
    """
    if not search_query:
        return query

    search_query = parse_search_query(search_query)
    if not search_query:
        return query

    entity = query._entities[0].entity_zero.class_

    if not vector:
        search_vectors = inspect_search_vectors(entity)
        vector = search_vectors[0]

    query = query.filter(
        vector.match_tsquery(search_query, catalog=catalog)
    )
    return query.params(term=search_query)


def quote_identifier(identifier):
    """Adds double quotes to given identifier. Since PostgreSQL is the only
    supported dialect we don't need dialect specific stuff here"""
    return '"%s"' % identifier


class SQLConstruct(object):
    def __init__(self, column, options=None):
        self.table = column.table
        self.column = column
        if not options:
           options = {}
        for key, value in SearchManager.default_options.items():
            try:
                option = self.column.type.options[key]
            except (KeyError, AttributeError):
                option = value
            options.setdefault(key, option)
        self.options = options

    @property
    def table_name(self):
        if self.table.schema:
            return '%s."%s"' % (self.table.schema, self.table.name)
        else:
            return '"' + self.table.name + '"'

    @property
    def search_index_name(self):
        return self.options['search_index_name'].format(
            table=self.table.name,
            column=self.column.name
        )

    @property
    def search_function_name(self):
        return self.options['search_trigger_function_name'].format(
            table=self.column.table.name,
            column=self.column.name
        )

    @property
    def search_trigger_name(self):
        return self.options['search_trigger_name'].format(
            table=self.column.table.name,
            column=self.column.name
        )

    @property
    def search_function_args(self):
        return 'CONCAT(%s)' % ', '.join(
            "REPLACE(COALESCE(NEW.%s, ''), '-', ' '), ' '" % column_name
            for column_name in list(self.column.type.columns)
        )


class CreateSearchFunctionSQL(SQLConstruct):
    def __str__(self):
        return (
            """CREATE FUNCTION
                {search_trigger_function_name}() RETURNS TRIGGER AS $$
            BEGIN
                NEW.{search_vector_name} = to_tsvector(
                    {arguments}
                );
                RETURN NEW;
            END
            $$ LANGUAGE 'plpgsql';
            """
        ).format(
            search_trigger_function_name=self.search_function_name,
            search_vector_name=self.column.name,
            arguments="'%s', %s" % (
                self.options['catalog'],
                self.search_function_args
            )
        )


class CreateSearchTriggerSQL(SQLConstruct):
    @property
    def search_trigger_function_with_trigger_args(self):
        if self.options['remove_hyphens']:
            return self.search_function_name + '()'
        return 'tsvector_update_trigger({arguments})'.format(
            arguments=', '.join(
                [
                    self.column.name,
                    "'%s'" % self.options['catalog']
                ] +
                list(self.column.type.columns)
            )
        )

    def __str__(self):
        return (
            "CREATE TRIGGER {search_trigger_name}"
            " BEFORE UPDATE OR INSERT ON {table}"
            " FOR EACH ROW EXECUTE PROCEDURE"
            " {procedure_ddl}"
            .format(
                search_trigger_name=self.search_trigger_name,
                table=self.table_name,
                procedure_ddl=
                self.search_trigger_function_with_trigger_args
            )
        )


class CreateSearchIndexSQL(SQLConstruct):
    def __str__(self):
        return (
            "CREATE INDEX {search_index_name} ON {table}"
            " USING gin({search_vector_name})"
            .format(
                table=self.table_name,
                search_index_name=self.search_index_name,
                search_vector_name=self.column.name
            )
        )


class DropSearchFunctionSQL(SQLConstruct):
    def __str__(self):
        return 'DROP FUNCTION IF EXISTS %s()' % self.search_function_name


class SearchManager():
    default_options = {
        'tablename': None,
        'remove_hyphens': True,
        'search_trigger_name': '{table}_{column}_trigger',
        'search_index_name': '{table}_{column}_index',
        'search_trigger_function_name': '{table}_{column}_update',
        'catalog': 'pg_catalog.english'
    }

    def __init__(self, options={}):
        self.options = self.default_options
        self.options.update(options)
        self.processed_columns = []

    def option(self, column, name):
        try:
            return column.type.options[name]
        except (AttributeError, KeyError):
            return self.options[name]

    def search_index_ddl(self, column):
        """
        Returns the ddl for creating the actual search index.

        :param column: TSVectorType typed SQLAlchemy column object
        """
        return DDL(str(CreateSearchIndexSQL(column)))

    def search_function_ddl(self, column):
        return DDL(str(CreateSearchFunctionSQL(column)))

    def search_trigger_ddl(self, column):
        """
        Returns the ddl for creating an automatically updated search trigger.

        :param column: TSVectorType typed SQLAlchemy column object
        """
        return DDL(str(CreateSearchTriggerSQL(column)))

    def inspect_columns(self, cls):
        """
        Inspects all searchable columns for given class.

        :param cls: SQLAlchemy declarative class
        """
        return [
            column for column in cls.__table__.c
            if isinstance(column.type, TSVectorType)
        ]

    def attach_ddl_listeners(self, mapper, cls):
        columns = self.inspect_columns(cls)
        for column in columns:
            # We don't want sqlalchemy to know about this column so we add it
            # externally.
            table = cls.__table__

            column_name = '%s_%s' % (table.name, column.name)

            if column_name in self.processed_columns:
                continue

            # This indexes the tsvector column.
            event.listen(
                table,
                'after_create',
                self.search_index_ddl(column)
            )

            # This sets up the trigger that keeps the tsvector column up to
            # date.
            if column.type.columns:
                if self.option(column, 'remove_hyphens'):
                    event.listen(
                        table,
                        'after_create',
                        self.search_function_ddl(column)
                    )
                    event.listen(
                        table,
                        'after_drop',
                        DDL(str(DropSearchFunctionSQL(column)))
                    )
                event.listen(
                    table,
                    'after_create',
                    self.search_trigger_ddl(column)
                )

            self.processed_columns.append(column_name)


search_manager = SearchManager()


def make_searchable(
    mapper=sa.orm.mapper,
    manager=search_manager,
    options={}
):
    manager.options.update(options)
    event.listen(
        mapper, 'instrument_class', manager.attach_ddl_listeners
    )
