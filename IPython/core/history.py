""" History related magics and functionality """
#-----------------------------------------------------------------------------
#  Copyright (C) 2010-2011 The IPython Development Team.
#
#  Distributed under the terms of the BSD License.
#
#  The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------
from __future__ import print_function

# Stdlib imports
import atexit
import datetime
import os
import re
try:
    import sqlite3
except ImportError:
    try:
        from pysqlite2 import dbapi2 as sqlite3
    except ImportError:
        sqlite3 = None
import threading

# Our own packages
from IPython.config.configurable import Configurable
from IPython.external.decorator import decorator
from IPython.utils.decorators import undoc
from IPython.utils.path import locate_profile
from IPython.utils import py3compat
from IPython.utils.traitlets import (
    Any, Bool, Dict, Instance, Integer, List, Unicode, TraitError,
)
from IPython.utils.warn import warn

#-----------------------------------------------------------------------------
# Classes and functions
#-----------------------------------------------------------------------------

@undoc
class DummyDB(object):
    """Dummy DB that will act as a black hole for history.
    
    Only used in the absence of sqlite"""
    def execute(*args, **kwargs):
        return []
    
    def commit(self, *args, **kwargs):
        pass
    
    def __enter__(self, *args, **kwargs):
        pass
    
    def __exit__(self, *args, **kwargs):
        pass


@decorator
def needs_sqlite(f, self, *a, **kw):
    """Decorator: return an empty list in the absence of sqlite."""
    if sqlite3 is None or not self.enabled:
        return []
    else:
        return f(self, *a, **kw)


if sqlite3 is not None:
    DatabaseError = sqlite3.DatabaseError
else:
    @undoc
    class DatabaseError(Exception):
        "Dummy exception when sqlite could not be imported. Should never occur."

@decorator
def catch_corrupt_db(f, self, *a, **kw):
    """A decorator which wraps HistoryAccessor method calls to catch errors from
    a corrupt SQLite database, move the old database out of the way, and create
    a new one.
    """
    try:
        return f(self, *a, **kw)
    except DatabaseError:
        if os.path.isfile(self.hist_file):
            # Try to move the file out of the way
            base,ext = os.path.splitext(self.hist_file)
            newpath = base + '-corrupt' + ext
            os.rename(self.hist_file, newpath)
            self.init_db()
            print("ERROR! History file wasn't a valid SQLite database.",
            "It was moved to %s" % newpath, "and a new file created.")
            return []
        
        else:
            # The hist_file is probably :memory: or something else.
            raise
        


class HistoryAccessor(Configurable):
    """Access the history database without adding to it.
    
    This is intended for use by standalone history tools. IPython shells use
    HistoryManager, below, which is a subclass of this."""

    # String holding the path to the history file
    hist_file = Unicode(config=True,
        help="""Path to file to use for SQLite history database.
        
        By default, IPython will put the history database in the IPython
        profile directory.  If you would rather share one history among
        profiles, you can set this value in each, so that they are consistent.
        
        Due to an issue with fcntl, SQLite is known to misbehave on some NFS
        mounts.  If you see IPython hanging, try setting this to something on a
        local disk, e.g::
        
            ipython --HistoryManager.hist_file=/tmp/ipython_hist.sqlite
        
        """)
    
    enabled = Bool(True, config=True,
        help="""enable the SQLite history
        
        set enabled=False to disable the SQLite history,
        in which case there will be no stored history, no SQLite connection,
        and no background saving thread.  This may be necessary in some
        threaded environments where IPython is embedded.
        """
    )
    
    connection_options = Dict(config=True,
        help="""Options for configuring the SQLite connection
        
        These options are passed as keyword args to sqlite3.connect
        when establishing database conenctions.
        """
    )

    # The SQLite database
    db = Any()
    def _db_changed(self, name, old, new):
        """validate the db, since it can be an Instance of two different types"""
        connection_types = (DummyDB,)
        if sqlite3 is not None:
            connection_types = (DummyDB, sqlite3.Connection)
        if not isinstance(new, connection_types):
            msg = "%s.db must be sqlite3 Connection or DummyDB, not %r" % \
                    (self.__class__.__name__, new)
            raise TraitError(msg)
    
    def __init__(self, profile='default', hist_file=u'', **traits):
        """Create a new history accessor.
        
        Parameters
        ----------
        profile : str
          The name of the profile from which to open history.
        hist_file : str
          Path to an SQLite history database stored by IPython. If specified,
          hist_file overrides profile.
        config : :class:`~IPython.config.loader.Config`
          Config object. hist_file can also be set through this.
        """
        # We need a pointer back to the shell for various tasks.
        super(HistoryAccessor, self).__init__(**traits)
        # defer setting hist_file from kwarg until after init,
        # otherwise the default kwarg value would clobber any value
        # set by config
        if hist_file:
            self.hist_file = hist_file
        
        if self.hist_file == u'':
            # No one has set the hist_file, yet.
            self.hist_file = self._get_hist_file_name(profile)

        if sqlite3 is None and self.enabled:
            warn("IPython History requires SQLite, your history will not be saved")
            self.enabled = False
        
        self.init_db()
    
    def _get_hist_file_name(self, profile='default'):
        """Find the history file for the given profile name.
        
        This is overridden by the HistoryManager subclass, to use the shell's
        active profile.
        
        Parameters
        ----------
        profile : str
          The name of a profile which has a history file.
        """
        return os.path.join(locate_profile(profile), 'history.sqlite')
    
    @catch_corrupt_db
    def init_db(self):
        """Connect to the database, and create tables if necessary."""
        if not self.enabled:
            self.db = DummyDB()
            return
        
        # use detect_types so that timestamps return datetime objects
        kwargs = dict(detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES,
                      check_same_thread=False)
        kwargs.update(self.connection_options)
        self.db = sqlite3.connect(self.hist_file, **kwargs)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (session integer
                        primary key autoincrement, start timestamp,
                        end timestamp, num_cmds integer, remark text)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS history
                (session integer, line integer, source text, source_raw text,
                PRIMARY KEY (session, line))""")
        # Output history is optional, but ensure the table's there so it can be
        # enabled later.
        self.db.execute("""CREATE TABLE IF NOT EXISTS output_history
                        (session integer, line integer, output text,
                        PRIMARY KEY (session, line))""")
        self.db.commit()

    def writeout_cache(self):
        """Overridden by HistoryManager to dump the cache before certain
        database lookups."""
        pass

    ## -------------------------------
    ## Methods for retrieving history:
    ## -------------------------------
    def _run_sql(self, sql, params, raw=True, output=False):
        """Prepares and runs an SQL query for the history database.

        Parameters
        ----------
        sql : str
          Any filtering expressions to go after SELECT ... FROM ...
        params : tuple
          Parameters passed to the SQL query (to replace "?")
        raw, output : bool
          See :meth:`get_range`

        Returns
        -------
        Tuples as :meth:`get_range`
        """
        toget = 'source_raw' if raw else 'source'
        sqlfrom = "history"
        if output:
            sqlfrom = "history LEFT JOIN output_history USING (session, line)"
            toget = "history.%s, output_history.output" % toget
        cur = self.db.execute("SELECT session, line, %s FROM %s " %\
                                (toget, sqlfrom) + sql, params)
        if output:    # Regroup into 3-tuples, and parse JSON
            return ((ses, lin, (inp, out)) for ses, lin, inp, out in cur)
        return cur

    @needs_sqlite
    @catch_corrupt_db
    def get_session_info(self, session):
        """Get info about a session.

        Parameters
        ----------

        session : int
            Session number to retrieve.

        Returns
        -------
        
        session_id : int
           Session ID number
        start : datetime
           Timestamp for the start of the session.
        end : datetime
           Timestamp for the end of the session, or None if IPython crashed.
        num_cmds : int
           Number of commands run, or None if IPython crashed.
        remark : unicode
           A manually set description.
        """
        query = "SELECT * from sessions where session == ?"
        return self.db.execute(query, (session,)).fetchone()

    @catch_corrupt_db
    def get_last_session_id(self):
        """Get the last session ID currently in the database.
        
        Within IPython, this should be the same as the value stored in
        :attr:`HistoryManager.session_number`.
        """
        for record in self.get_tail(n=1, include_latest=True):
            return record[0]

    @catch_corrupt_db
    def get_tail(self, n=10, raw=True, output=False, include_latest=False):
        """Get the last n lines from the history database.

        Parameters
        ----------
        n : int
          The number of lines to get
        raw, output : bool
          See :meth:`get_range`
        include_latest : bool
          If False (default), n+1 lines are fetched, and the latest one
          is discarded. This is intended to be used where the function
          is called by a user command, which it should not return.

        Returns
        -------
        Tuples as :meth:`get_range`
        """
        self.writeout_cache()
        if not include_latest:
            n += 1
        cur = self._run_sql("ORDER BY session DESC, line DESC LIMIT ?",
                                (n,), raw=raw, output=output)
        if not include_latest:
            return reversed(list(cur)[1:])
        return reversed(list(cur))

    @catch_corrupt_db
    def search(self, pattern="*", raw=True, search_raw=True,
               output=False, n=None, unique=False):
        """Search the database using unix glob-style matching (wildcards
        * and ?).

        Parameters
        ----------
        pattern : str
          The wildcarded pattern to match when searching
        search_raw : bool
          If True, search the raw input, otherwise, the parsed input
        raw, output : bool
          See :meth:`get_range`
        n : None or int
          If an integer is given, it defines the limit of
          returned entries.
        unique : bool
          When it is true, return only unique entries.

        Returns
        -------
        Tuples as :meth:`get_range`
        """
        tosearch = "source_raw" if search_raw else "source"
        if output:
            tosearch = "history." + tosearch
        self.writeout_cache()
        sqlform = "WHERE %s GLOB ?" % tosearch
        params = (pattern,)
        if unique:
            sqlform += ' GROUP BY {0}'.format(tosearch)
        if n is not None:
            sqlform += " ORDER BY session DESC, line DESC LIMIT ?"
            params += (n,)
        elif unique:
            sqlform += " ORDER BY session, line"
        cur = self._run_sql(sqlform, params, raw=raw, output=output)
        if n is not None:
            return reversed(list(cur))
        return cur
    
    @catch_corrupt_db
    def get_range(self, session, start=1, stop=None, raw=True,output=False):
        """Retrieve input by session.

        Parameters
        ----------
        session : int
            Session number to retrieve.
        start : int
            First line to retrieve.
        stop : int
            End of line range (excluded from output itself). If None, retrieve
            to the end of the session.
        raw : bool
            If True, return untranslated input
        output : bool
            If True, attempt to include output. This will be 'real' Python
            objects for the current session, or text reprs from previous
            sessions if db_log_output was enabled at the time. Where no output
            is found, None is used.

        Returns
        -------
        entries
          An iterator over the desired lines. Each line is a 3-tuple, either
          (session, line, input) if output is False, or
          (session, line, (input, output)) if output is True.
        """
        if stop:
            lineclause = "line >= ? AND line < ?"
            params = (session, start, stop)
        else:
            lineclause = "line>=?"
            params = (session, start)

        return self._run_sql("WHERE session==? AND %s" % lineclause,
                                    params, raw=raw, output=output)

    def get_range_by_str(self, rangestr, raw=True, output=False):
        """Get lines of history from a string of ranges, as used by magic
        commands %hist, %save, %macro, etc.

        Parameters
        ----------
        rangestr : str
          A string specifying ranges, e.g. "5 ~2/1-4". See
          :func:`magic_history` for full details.
        raw, output : bool
          As :meth:`get_range`

        Returns
        -------
        Tuples as :meth:`get_range`
        """
        for sess, s, e in extract_hist_ranges(rangestr):
            for line in self.get_range(sess, s, e, raw=raw, output=output):
                yield line


class HistoryManager(HistoryAccessor):
    """A class to organize all history-related functionality in one place.
    """
    # Public interface

    # An instance of the IPython shell we are attached to
    shell = Instance('IPython.core.interactiveshell.InteractiveShellABC')
    # Lists to hold processed and raw history. These start with a blank entry
    # so that we can index them starting from 1
    input_hist_parsed = List([""])
    input_hist_raw = List([""])
    # A list of directories visited during session
    dir_hist = List()
    def _dir_hist_default(self):
        try:
            return [py3compat.getcwd()]
        except OSError:
            return []

    # A dict of output history, keyed with ints from the shell's
    # execution count.
    output_hist = Dict()
    # The text/plain repr of outputs.
    output_hist_reprs = Dict()

    # The number of the current session in the history database
    session_number = Integer()
    
    db_log_output = Bool(False, config=True,
        help="Should the history database include output? (default: no)"
    )
    db_cache_size = Integer(0, config=True,
        help="Write to database every x commands (higher values save disk access & power).\n"
        "Values of 1 or less effectively disable caching."
    )
    # The input and output caches
    db_input_cache = List()
    db_output_cache = List()
    
    # History saving in separate thread
    save_thread = Instance('IPython.core.history.HistorySavingThread')
    try:               # Event is a function returning an instance of _Event...
        save_flag = Instance(threading._Event)
    except AttributeError:         # ...until Python 3.3, when it's a class.
        save_flag = Instance(threading.Event)
    
    # Private interface
    # Variables used to store the three last inputs from the user.  On each new
    # history update, we populate the user's namespace with these, shifted as
    # necessary.
    _i00 = Unicode(u'')
    _i = Unicode(u'')
    _ii = Unicode(u'')
    _iii = Unicode(u'')

    # A regex matching all forms of the exit command, so that we don't store
    # them in the history (it's annoying to rewind the first entry and land on
    # an exit call).
    _exit_re = re.compile(r"(exit|quit)(\s*\(.*\))?$")

    def __init__(self, shell=None, config=None, **traits):
        """Create a new history manager associated with a shell instance.
        """
        # We need a pointer back to the shell for various tasks.
        super(HistoryManager, self).__init__(shell=shell, config=config,
            **traits)
        self.save_flag = threading.Event()
        self.db_input_cache_lock = threading.Lock()
        self.db_output_cache_lock = threading.Lock()
        if self.enabled and self.hist_file != ':memory:':
            self.save_thread = HistorySavingThread(self)
            self.save_thread.start()

        self.new_session()

    def _get_hist_file_name(self, profile=None):
        """Get default history file name based on the Shell's profile.
        
        The profile parameter is ignored, but must exist for compatibility with
        the parent class."""
        profile_dir = self.shell.profile_dir.location
        return os.path.join(profile_dir, 'history.sqlite')
    
    @needs_sqlite
    def new_session(self, conn=None):
        """Get a new session number."""
        if conn is None:
            conn = self.db
        
        with conn:
            cur = conn.execute("""INSERT INTO sessions VALUES (NULL, ?, NULL,
                            NULL, "") """, (datetime.datetime.now(),))
            self.session_number = cur.lastrowid
            
    def end_session(self):
        """Close the database session, filling in the end time and line count."""
        self.writeout_cache()
        with self.db:
            self.db.execute("""UPDATE sessions SET end=?, num_cmds=? WHERE
                            session==?""", (datetime.datetime.now(),
                            len(self.input_hist_parsed)-1, self.session_number))
        self.session_number = 0
                            
    def name_session(self, name):
        """Give the current session a name in the history database."""
        with self.db:
            self.db.execute("UPDATE sessions SET remark=? WHERE session==?",
                            (name, self.session_number))
                            
    def reset(self, new_session=True):
        """Clear the session history, releasing all object references, and
        optionally open a new session."""
        self.output_hist.clear()
        # The directory history can't be completely empty
        self.dir_hist[:] = [py3compat.getcwd()]
        
        if new_session:
            if self.session_number:
                self.end_session()
            self.input_hist_parsed[:] = [""]
            self.input_hist_raw[:] = [""]
            self.new_session()
    
    # ------------------------------
    # Methods for retrieving history
    # ------------------------------
    def get_session_info(self, session=0):
        """Get info about a session.

        Parameters
        ----------

        session : int
            Session number to retrieve. The current session is 0, and negative
            numbers count back from current session, so -1 is the previous session.

        Returns
        -------
        
        session_id : int
           Session ID number
        start : datetime
           Timestamp for the start of the session.
        end : datetime
           Timestamp for the end of the session, or None if IPython crashed.
        num_cmds : int
           Number of commands run, or None if IPython crashed.
        remark : unicode
           A manually set description.
        """
        if session <= 0:
            session += self.session_number

        return super(HistoryManager, self).get_session_info(session=session)

    def _get_range_session(self, start=1, stop=None, raw=True, output=False):
        """Get input and output history from the current session. Called by
        get_range, and takes similar parameters."""
        input_hist = self.input_hist_raw if raw else self.input_hist_parsed
            
        n = len(input_hist)
        if start < 0:
            start += n
        if not stop or (stop > n):
            stop = n
        elif stop < 0:
            stop += n
        
        for i in range(start, stop):
            if output:
                line = (input_hist[i], self.output_hist_reprs.get(i))
            else:
                line = input_hist[i]
            yield (0, i, line)
    
    def get_range(self, session=0, start=1, stop=None, raw=True,output=False):
        """Retrieve input by session.
        
        Parameters
        ----------
        session : int
            Session number to retrieve. The current session is 0, and negative
            numbers count back from current session, so -1 is previous session.
        start : int
            First line to retrieve.
        stop : int
            End of line range (excluded from output itself). If None, retrieve
            to the end of the session.
        raw : bool
            If True, return untranslated input
        output : bool
            If True, attempt to include output. This will be 'real' Python
            objects for the current session, or text reprs from previous
            sessions if db_log_output was enabled at the time. Where no output
            is found, None is used.
            
        Returns
        -------
        entries
          An iterator over the desired lines. Each line is a 3-tuple, either
          (session, line, input) if output is False, or
          (session, line, (input, output)) if output is True.
        """
        if session <= 0:
            session += self.session_number
        if session==self.session_number:          # Current session
            return self._get_range_session(start, stop, raw, output)
        return super(HistoryManager, self).get_range(session, start, stop, raw,
                                                     output)

    ## ----------------------------
    ## Methods for storing history:
    ## ----------------------------
    def store_inputs(self, line_num, source, source_raw=None):
        """Store source and raw input in history and create input cache
        variables ``_i*``.

        Parameters
        ----------
        line_num : int
          The prompt number of this input.

        source : str
          Python input.

        source_raw : str, optional
          If given, this is the raw input without any IPython transformations
          applied to it.  If not given, ``source`` is used.
        """
        if source_raw is None:
            source_raw = source
        source = source.rstrip('\n')
        source_raw = source_raw.rstrip('\n')

        # do not store exit/quit commands
        if self._exit_re.match(source_raw.strip()):
            return

        self.input_hist_parsed.append(source)
        self.input_hist_raw.append(source_raw)

        with self.db_input_cache_lock:
            self.db_input_cache.append((line_num, source, source_raw))
            # Trigger to flush cache and write to DB.
            if len(self.db_input_cache) >= self.db_cache_size:
                self.save_flag.set()

        # update the auto _i variables
        self._iii = self._ii
        self._ii = self._i
        self._i = self._i00
        self._i00 = source_raw

        # hackish access to user namespace to create _i1,_i2... dynamically
        new_i = '_i%s' % line_num
        to_main = {'_i': self._i,
                   '_ii': self._ii,
                   '_iii': self._iii,
                   new_i : self._i00 }
        
        if self.shell is not None:
            self.shell.push(to_main, interactive=False)

    def store_output(self, line_num):
        """If database output logging is enabled, this saves all the
        outputs from the indicated prompt number to the database. It's
        called by run_cell after code has been executed.

        Parameters
        ----------
        line_num : int
          The line number from which to save outputs
        """
        if (not self.db_log_output) or (line_num not in self.output_hist_reprs):
            return
        output = self.output_hist_reprs[line_num]

        with self.db_output_cache_lock:
            self.db_output_cache.append((line_num, output))
        if self.db_cache_size <= 1:
            self.save_flag.set()

    def _writeout_input_cache(self, conn):
        with conn:
            for line in self.db_input_cache:
                conn.execute("INSERT INTO history VALUES (?, ?, ?, ?)",
                                (self.session_number,)+line)

    def _writeout_output_cache(self, conn):
        with conn:
            for line in self.db_output_cache:
                conn.execute("INSERT INTO output_history VALUES (?, ?, ?)",
                                (self.session_number,)+line)

    @needs_sqlite
    def writeout_cache(self, conn=None):
        """Write any entries in the cache to the database."""
        if conn is None:
            conn = self.db

        with self.db_input_cache_lock:
            try:
                self._writeout_input_cache(conn)
            except sqlite3.IntegrityError:
                self.new_session(conn)
                print("ERROR! Session/line number was not unique in",
                      "database. History logging moved to new session",
                                                self.session_number)
                try:
                    # Try writing to the new session. If this fails, don't
                    # recurse
                    self._writeout_input_cache(conn)
                except sqlite3.IntegrityError:
                    pass
            finally:
                self.db_input_cache = []

        with self.db_output_cache_lock:
            try:
                self._writeout_output_cache(conn)
            except sqlite3.IntegrityError:
                print("!! Session/line number for output was not unique",
                      "in database. Output will not be stored.")
            finally:
                self.db_output_cache = []


class HistorySavingThread(threading.Thread):
    """This thread takes care of writing history to the database, so that
    the UI isn't held up while that happens.

    It waits for the HistoryManager's save_flag to be set, then writes out
    the history cache. The main thread is responsible for setting the flag when
    the cache size reaches a defined threshold."""
    daemon = True
    stop_now = False
    enabled = True
    def __init__(self, history_manager):
        super(HistorySavingThread, self).__init__(name="IPythonHistorySavingThread")
        self.history_manager = history_manager
        self.enabled = history_manager.enabled
        atexit.register(self.stop)

    @needs_sqlite
    def run(self):
        # We need a separate db connection per thread:
        try:
            self.db = sqlite3.connect(self.history_manager.hist_file,
                            **self.history_manager.connection_options
            )
            while True:
                self.history_manager.save_flag.wait()
                if self.stop_now:
                    return
                self.history_manager.save_flag.clear()
                self.history_manager.writeout_cache(self.db)
        except Exception as e:
            print(("The history saving thread hit an unexpected error (%s)."
                   "History will not be written to the database.") % repr(e))

    def stop(self):
        """This can be called from the main thread to safely stop this thread.

        Note that it does not attempt to write out remaining history before
        exiting. That should be done by calling the HistoryManager's
        end_session method."""
        self.stop_now = True
        self.history_manager.save_flag.set()
        self.join()


# To match, e.g. ~5/8-~2/3
range_re = re.compile(r"""
((?P<startsess>~?\d+)/)?
(?P<start>\d+)?
((?P<sep>[\-:])
 ((?P<endsess>~?\d+)/)?
 (?P<end>\d+))?
$""", re.VERBOSE)


def extract_hist_ranges(ranges_str):
    """Turn a string of history ranges into 3-tuples of (session, start, stop).

    Examples
    --------
    >>> list(extract_hist_ranges("~8/5-~7/4 2"))
    [(-8, 5, None), (-7, 1, 5), (0, 2, 3)]
    """
    for range_str in ranges_str.split():
        rmatch = range_re.match(range_str)
        if not rmatch:
            continue
        start = rmatch.group("start")
        if start:
            start = int(start)
            end = rmatch.group("end")
            # If no end specified, get (a, a + 1)
            end = int(end) if end else start + 1
        else:  # start not specified
            if not rmatch.group('startsess'):  # no startsess
                continue
            start = 1
            end = None  # provide the entire session hist

        if rmatch.group("sep") == "-":       # 1-3 == 1:4 --> [1, 2, 3]
            end += 1
        startsess = rmatch.group("startsess") or "0"
        endsess = rmatch.group("endsess") or startsess
        startsess = int(startsess.replace("~","-"))
        endsess = int(endsess.replace("~","-"))
        assert endsess >= startsess, "start session must be earlier than end session"

        if endsess == startsess:
            yield (startsess, start, end)
            continue
        # Multiple sessions in one range:
        yield (startsess, start, None)
        for sess in range(startsess+1, endsess):
            yield (sess, 1, None)
        yield (endsess, 1, end)


def _format_lineno(session, line):
    """Helper function to format line numbers properly."""
    if session == 0:
        return str(line)
    return "%s#%s" % (session, line)


