import re
import sys

import sqlalchemy as s
from dateutil.parser import parse as parse_dt
from sqlalchemy import orm, func
from sqlalchemy.sql.expression import label

from .log import Loggable


class Database(Loggable):
  def __init__(self, config):
    super(Database, self).__init__()

    # pool_recycle: you'll get 'MySQL server has gone away' errors after a few
    #   hours if you don't enable. See,
    #   http://docs.sqlalchemy.org/en/rel_0_8/faq.html#mysql-server-has-gone-away
    engine            = s.create_engine(config.uri, pool_recycle=300)

    self.config       = config
    self.meta         = s.MetaData(bind=engine)
    self.sessionmaker = orm.sessionmaker(bind=engine)

    # discover what tables are available
    self.meta.reflect(engine)
    self.log.info("Initialized database: %s", config.uri.rsplit("/", 1)[-1])

  def aggregate(self, table, groupby, filters={}, aggregate='count(*)', page=0, page_size=100, orderby=None):
    self.log.info((
        "table=%s, groupby=%s, filters=%s, aggregate=%s, page=%s,"
        " page_size=%s, orderby=%s"
      ), table, groupby, filters, aggregate, page, page_size, orderby)
    table_  = self._table(table)
    columnd = { col.name:col for col in table_.columns }

    if isinstance(groupby, basestring):
      groupby = [ groupby ]
    if isinstance(aggregate, basestring):
      aggregate = [ aggregate ]

    session = self.sessionmaker()
    try:
      groupby_    = [ label(c, str2col(c, table_)) for c in groupby ]
      aggregate_  = [ label(a, str2col(a, table_)) for a in aggregate ]

      query = session.query(*(aggregate_ + groupby_))
      query = with_filters(query, table_, filters)
      query = query.group_by(*groupby_)
      query = with_orderby(query, table_, orderby)
      query = with_pagination(query, table_, page, page_size)
      result = result2dict(query.all())

      self.log.info("retrieved %d rows", len(result))
      return result

    finally:
      session.close()

  def select(self, table, columns=None, filters={}, page=0, page_size=100, orderby=None):
    self.log.info((
        "table=%s, columns=%s, filters=%s, page=%s, page_size=%s, orderby=%s"
      ), table, columns, filters, page, page_size, orderby)
    table_  = self._table(table)
    columnd = { c.name:c for c in table_.columns }

    # get column objects corresponding to names
    if isinstance(columns, basestring):
      columns = [ columns ]

    if columns is None:
      columns_ = list(table_.columns)
    else:
      columns_ = [label(c, str2col(c, table_)) for c in columns]

    session = self.sessionmaker()
    try:
      query = session.query(*columns_)
      query = with_filters(query, table_, filters)
      query = with_orderby(query, table_, orderby)
      query = with_pagination(query, table_, page, page_size)
      result = result2dict(query.all())
      self.log.info("retrieved %d rows", len(result))
      return result
    finally:
      session.close()

  def insert(self, table, rows):
    table_  = self._table(table)
    session = self.sessionmaker()
    try:
      # coerce columns based on table_'s types
      rows = [dict2row(table_, row) for row in rows]
      session.execute(table_.insert().values(rows))
      session.commit()
      return {
          'status': 'success',
          'n_rows': len(rows)
      }
    finally:
      session.close()

  def delete(self, table, filters):
    table_  = self._table(table)
    session = self.sessionmaker()
    try:
      # count number of rows to be deleted
      n = self.count(table, filters)

      # actually delete rows
      session.execute(
        table_.delete(where_clause(filters, table_))
      )
      session.commit()

      return {
          'status': 'success',
          'n_rows': n
      }
    finally:
      session.close()

  def count(self, table, filters):
    table_  = self._table(table)
    session = self.sessionmaker()
    try:
      # get number of rows to be deleted (this isn't threadsafe)
      query = session.query(func.count(list(table_.columns)[0]))
      query = with_filters(query, table_, filters)
      return query.all()[0][0]
    finally:
      session.close()

  def update(self, table, filters, values):
    table_  = self._table(table)
    session = self.sessionmaker()
    try:
      n = self.count(table, filters)

      session.execute(
        table_
          .update()
          .where(where_clause(filters, table_))
          .values(dict2row(table_, values))
      )
      session.commit()

      return {
          'status': 'success',
          'n_rows': n
      }
    finally:
      session.close()

  def tables(self):
    self.log.info("")
    return self.meta.tables.keys()

  def columns(self, table):
    self.log.info("table=%s", table)
    columns = []
    for column in self._table(table).columns:
      columns.append({
        'name'        : column.name,
        'type'        : str(column.type),
        'description' : column.description,
      })
    return columns

  def __str__(self):
    return "Database(%s)" % (self.config.uri,)

  def _table(self, table):
    return s.Table(table, self.meta, autoload=True)


def where_clause(filters, table_):
  """Transform a JSON blob into a where clause"""
  def iscontinuous(c):
    types     = [s.DateTime, s.Date, s.Time, s.SmallInteger, s.Integer, s.BigInteger, s.Float]
    functypes = {'date'}  # some functions have incorrect return types
    return any(isinstance(c.type, type_) for type_ in types) \
        or (isinstance(c, s.sql.expression.Function) and c.name.lower() in functypes)

  clauses = []
  # for each query attribute
  for k, v in filters.iteritems():
    c = str2col(k, table_)
    if isinstance(v, list):
      if iscontinuous(c) and len(v) == 2:
        # if it's a continuous field and there are 2 values, it's a range query
        clauses.append(c >= v[0])
        clauses.append(c  < v[1])
      else:
        # if the field isn't continuous or the number of values != 2, it's an "or" query
        clauses.append( s.or_(*[c == v_ for v_ in v]) )
    else:
      # otherwise, make an equality restriction
      clauses.append( c == v )

  return s.and_(*clauses)


def with_pagination(query, table_, page, page_size):
  return query.slice(page * page_size, (page + 1) * page_size)


def with_orderby(query, table_, orderby):
  if orderby is not None:
    if isinstance(orderby, list):
      orderby, direction = orderby
    else:
      direction = 'ascending'
    direction_ = s.desc if direction.lower() == 'descending' else s.asc
    query = query.order_by(direction_(str2col(orderby, table_)))
  return query


def with_filters(query, table_, filters):
  filters_ = where_clause(filters, table_)
  return query.filter(filters_)


def str2col(field, table):
  """Convert count(distinct(column)) into an actual query-able object"""
  # TODO this should be evaluated with a lexical parser
  # TODO functions of columns don't necessarily preserve type. e.g. DATE(...)
  # TODO don't recompute column name -> column mapping every time this function
  #      is called.

  # get column by name
  td = { col.name:col for col in table.columns }
  def getcolumn(k):
    if k == '*':
      # TODO this should be smarter. How do you resolve "*" in sqlalchemy?
      return td.values()[0]
    else:
      return td[k]

  # is this string a function call on a column?
  pattern = re.compile("^\s*(\w+)\((.+?)\)\s*$")
  def is_function(s):
    return pattern.search(s) is not None

  # get (function name, insides)
  def function_split(s):
    return pattern.search(s).groups()

  queue = []
  while is_function(field):
    funcname, field = function_split(field)
    queue.append(getattr(s.func, funcname))

  # all that's left is a single column
  column = getcolumn(field)

  # apply functions to column
  for f in reversed(queue):
    column = f(column)

  return column


def result2dict(r):
  """Convert SQLAlchemy's result set into an iterable of dicts"""
  result = []
  for e in r:
    result.append( { key:getattr(e, key) for key in e.keys() } )
  return result


def dict2row(table_, row):
  """Coerce `row`'s value types as necessary"""
  result = {}
  columns = { c.name: c for c in table_.columns }
  for k, v in row.items():
    if not k in columns:
      raise SqlRestException(
          "Column '{}' doesn't exist in table '{}'".format(k, table_.name)
      )

    # convert date types
    type = columns[k].type
    if isinstance(type, s.types.Date):
      v = parse_dt(v).date()
    elif isinstance(type, s.types.DateTime):
      v = parse_dt(v)

    # no need to convert boolean, numeric, or string types. only remaining type
    # is interval, but I don't have a good parser for it...
    result[k] = v

  return result


class SqlRestException(Exception):
  pass
