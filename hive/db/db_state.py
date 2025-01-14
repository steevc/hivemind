"""Hive db state manager. Check if schema loaded, init synced, etc."""

#pylint: disable=too-many-lines

import time
from time import perf_counter

import logging
import sqlalchemy

from hive.db.schema import (setup, set_logged_table_attribute, build_metadata,
                            build_metadata_community, teardown)
from hive.db.adapter import Db

from concurrent.futures import ThreadPoolExecutor, as_completed
from hive.indexer.auto_db_disposer import AutoDbDisposer

from hive.utils.post_active import update_active_starting_from_posts_on_block
from hive.utils.communities_rank import update_communities_posts_and_rank

from hive.server.common.payout_stats import PayoutStats

from hive.utils.stats import FinalOperationStatusManager as FOSM

log = logging.getLogger(__name__)

SYNCED_BLOCK_LIMIT = 7*24*1200 # 7 days

class DbState:
    """Manages database state: sync status, migrations, etc."""

    _db = None

    # prop is true until initial sync complete
    _is_initial_sync = True

    @classmethod
    def initialize(cls):
        """Perform startup database checks.

        1) Load schema if needed
        2) Run migrations if needed
        3) Check if initial sync has completed
        """

        log.info("[INIT] Welcome to hive!")

        # create db schema if needed
        if not cls._is_schema_loaded():
            log.info("[INIT] Create db schema...")
            setup(cls.db())

        # check if initial sync complete
        cls._is_initial_sync = True
        log.info("[INIT] Continue with initial sync...")

    @classmethod
    def teardown(cls):
        """Drop all tables in db."""
        teardown(cls.db())

    @classmethod
    def db(cls):
        """Get a db adapter instance."""
        if not cls._db:
            cls._db = Db.instance()
        return cls._db

    @classmethod
    def finish_initial_sync(cls, current_imported_block):
        """Set status to initial sync complete."""
        assert cls._is_initial_sync, "initial sync was not started."
        cls._after_initial_sync(current_imported_block)
        cls._is_initial_sync = False
        log.info("[INIT] Initial sync complete!")

    @classmethod
    def is_initial_sync(cls):
        """Check if we're still in the process of initial sync."""
        return cls._is_initial_sync

    @classmethod
    def _all_foreign_keys(cls):
        md = build_metadata()
        out = []
        for table in md.tables.values():
            out.extend(table.foreign_keys)
        return out

    @classmethod
    def _disableable_indexes(cls):
        to_locate = [
            'hive_blocks_created_at_idx',

            'hive_feed_cache_block_num_idx',
            'hive_feed_cache_created_at_idx',
            'hive_feed_cache_post_id_idx',

            'hive_follows_ix5a', # (following, state, created_at, follower)
            'hive_follows_ix5b', # (follower, state, created_at, following)
            'hive_follows_block_num_idx',
            'hive_follows_created_at_idx',

            'hive_posts_parent_id_id_idx',
            'hive_posts_depth_idx',
            'hive_posts_root_id_id_idx',

            'hive_posts_community_id_id_idx',
            'hive_posts_payout_at_idx',
            'hive_posts_payout_idx',
            'hive_posts_promoted_id_idx',
            'hive_posts_sc_trend_id_idx',
            'hive_posts_sc_hot_id_idx',
            'hive_posts_block_num_idx',
            'hive_posts_block_num_created_idx',
            'hive_posts_cashout_time_id_idx',
            'hive_posts_updated_at_idx',
            'hive_posts_payout_plus_pending_payout_id_idx',
            'hive_posts_category_id_payout_plus_pending_payout_depth_idx',
            'hive_posts_tags_ids_idx',
            'hive_posts_author_id_created_at_id_idx',
            'hive_posts_author_id_id_idx',


            'hive_posts_api_helper_author_s_permlink_idx',

            'hive_votes_voter_id_last_update_idx',
            'hive_votes_block_num_idx',

            'hive_subscriptions_block_num_idx',
            'hive_subscriptions_community_idx',
            'hive_communities_block_num_idx',
            'hive_reblogs_created_at_idx',

            'hive_votes_voter_id_post_id_idx',
            'hive_votes_post_id_voter_id_idx',

            'hive_reputation_data_block_num_idx',

            'hive_notification_cache_block_num_idx',
            'hive_notification_cache_dst_score_idx'
        ]

        to_return = {}
        md = build_metadata()
        for table in md.tables.values():
            for index in table.indexes:
                if index.name not in to_locate:
                    continue
                to_locate.remove(index.name)
                if table not in to_return:
                  to_return[ table ] = []
                to_return[ table ].append(index)

        # ensure we found all the items we expected
        assert not to_locate, "indexes not located: {}".format(to_locate)
        return to_return

    @classmethod
    def has_index(cls, db, idx_name):
        sql = "SELECT count(*) FROM pg_class WHERE relname = :relname"
        count = db.query_one(sql, relname=idx_name)
        if count == 1:
            return True
        else:
            return False

    @classmethod
    def _execute_query(cls, db, query):
        time_start = perf_counter()
   
        current_work_mem = cls.update_work_mem('2GB')
        log.info("[INIT] Attempting to execute query: `%s'...", query)

        row = db.query_no_return(query)

        cls.update_work_mem(current_work_mem)

        time_end = perf_counter()
        log.info("[INIT] Query `%s' done in %.4fs", query, time_end - time_start)


    @classmethod
    def processing_indexes_per_table(cls, db, table_name, indexes, is_pre_process, drop, create):
        log.info("[INIT] Begin %s-initial sync hooks for table %s", "pre" if is_pre_process else "post", table_name)
        with AutoDbDisposer(db, table_name) as db_mgr:
            engine = db_mgr.db.engine()

            any_index_created = False

            for index in indexes:
                log.info("%s index %s.%s", ("Drop" if is_pre_process else "Recreate"), index.table, index.name)
                try:
                    if drop:
                        if cls.has_index(db_mgr.db, index.name):
                            time_start = perf_counter()
                            index.drop(engine)
                            end_time = perf_counter()
                            elapsed_time = end_time - time_start
                            log.info("Index %s dropped in time %.4f s", index.name, elapsed_time)
                except sqlalchemy.exc.ProgrammingError as ex:
                    log.warning("Ignoring ex: {}".format(ex))

                if create:
                    if cls.has_index(db_mgr.db, index.name):
                        log.info("Index %s already exists... Creation skipped.", index.name)
                    else:
                        time_start = perf_counter()
                        index.create(engine)
                        end_time = perf_counter()
                        elapsed_time = end_time - time_start
                        log.info("Index %s created in time %.4f s", index.name, elapsed_time)
                        any_index_created = True
            if any_index_created:
                cls._execute_query(db_mgr.db,"ANALYZE")
        log.info("[INIT] End %s-initial sync hooks for table %s", "pre" if is_pre_process else "post", table_name)

    @classmethod
    def processing_indexes(cls, is_pre_process, drop, create):
        start_time = FOSM.start()
        _indexes = cls._disableable_indexes()

        methods = []
        for _key_table, indexes in _indexes.items():
          methods.append( (_key_table.name, cls.processing_indexes_per_table, [cls.db(), _key_table.name, indexes, is_pre_process, drop, create]) )

        cls.process_tasks_in_threads("[INIT] %i threads finished creating indexes.", methods)

        real_time = FOSM.stop(start_time)

        log.info("=== CREATING INDEXES ===")
        threads_time = FOSM.log_current("Total creating indexes time")
        log.info(f"Elapsed time: {real_time :.4f}s. Calculated elapsed time: {threads_time :.4f}s. Difference: {real_time - threads_time :.4f}s")
        FOSM.clear()
        log.info("=== CREATING INDEXES ===")

    @classmethod
    def before_initial_sync(cls, last_imported_block, hived_head_block):
        """Routine which runs *once* after db setup.

        Disables non-critical indexes for faster initial sync, as well
        as foreign key constraints."""

        to_sync = hived_head_block - last_imported_block

        if to_sync < SYNCED_BLOCK_LIMIT:
            log.info("[INIT] Skipping pre-initial sync hooks")
            return

        #is_pre_process, drop, create
        cls.processing_indexes( True, True, False )

        from hive.db.schema import drop_fk, set_logged_table_attribute
        log.info("Dropping FKs")
        drop_fk(cls.db())

        # intentionally disabled since it needs a lot of WAL disk space when switching back to LOGGED
        #set_logged_table_attribute(cls.db(), False)

        log.info("[INIT] Finish pre-initial sync hooks")

    @classmethod
    def update_work_mem(cls, workmem_value):
        row = cls.db().query_row("SHOW work_mem")
        current_work_mem = row['work_mem']

        sql = """
              DO $$
              BEGIN
                EXECUTE 'ALTER DATABASE '||current_database()||' SET work_mem TO "{}"';
              END
              $$;
              """
        cls.db().query_no_return(sql.format(workmem_value))

        return current_work_mem

    @classmethod
    def _finish_hive_posts(cls, db, massive_sync_preconditions, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_hive_posts") as db_mgr:
            def vacuum_hive_posts(cls):
              if massive_sync_preconditions:
                  cls._execute_query(db_mgr.db, "VACUUM ANALYZE hive_posts")

            #UPDATE: `children`
            time_start = perf_counter()
            if massive_sync_preconditions:
                # Update count of all child posts (what was hold during initial sync)
                cls._execute_query(db_mgr.db, "select update_all_hive_posts_children_count()")
            else:
                # Update count of child posts processed during partial sync (what was hold during initial sync)
                sql = "select update_hive_posts_children_count({}, {})".format(last_imported_block, current_imported_block)
                cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_hive_posts_children_count executed in %.4fs", perf_counter() - time_start)

            time_start = perf_counter()
            vacuum_hive_posts(cls)
            log.info("[INIT] VACUUM ANALYZE hive_posts executed in %.4fs", perf_counter() - time_start)

            #UPDATE: `root_id`
            # Update root_id all root posts
            time_start = perf_counter()
            sql = """
                  select update_hive_posts_root_id({}, {})
                  """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_hive_posts_root_id executed in %.4fs", perf_counter() - time_start)

            time_start = perf_counter()
            vacuum_hive_posts(cls)
            log.info("[INIT] VACUUM ANALYZE hive_posts executed in %.4fs", perf_counter() - time_start)

            #UPDATE: `active`
            time_start = perf_counter()
            update_active_starting_from_posts_on_block(last_imported_block, current_imported_block)
            log.info("[INIT] update_all_posts_active executed in %.4fs", perf_counter() - time_start)

            time_start = perf_counter()
            vacuum_hive_posts(cls)
            log.info("[INIT] VACUUM ANALYZE hive_posts executed in %.4fs", perf_counter() - time_start)

            #UPDATE: `abs_rshares`, `vote_rshares`, `sc_hot`, ,`sc_trend`, `total_votes`, `net_votes`
            time_start = perf_counter()
            sql = """
                  SELECT update_posts_rshares({}, {});
                  """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_posts_rshares executed in %.4fs", perf_counter() - time_start)

            time_start = perf_counter()
            vacuum_hive_posts(cls)
            log.info("[INIT] VACUUM ANALYZE hive_posts executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_hive_posts_api_helper(cls, db, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_hive_posts_api_helper") as db_mgr:
            time_start = perf_counter()
            sql = """
                  select update_hive_posts_api_helper({}, {})
                  """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_hive_posts_api_helper executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_hive_feed_cache(cls, db, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_hive_feed_cache") as db_mgr:
            time_start = perf_counter()
            sql = """
                SELECT update_feed_cache({}, {});
            """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_feed_cache executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_hive_mentions(cls, db, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_hive_mentions") as db_mgr:
            time_start = perf_counter()
            sql = """
                SELECT update_hive_posts_mentions({}, {});
            """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_hive_posts_mentions executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_payout_stats_view(cls):
        time_start = perf_counter()
        PayoutStats.generate()
        log.info("[INIT] payout_stats_view executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_account_reputations(cls, db, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_account_reputations") as db_mgr:
            time_start = perf_counter()
            sql = """
                  SELECT update_account_reputations({}, {}, True);
                  """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_account_reputations executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_communities_posts_and_rank(cls, db):
        with AutoDbDisposer(db, "finish_communities_posts_and_rank") as db_mgr:
            time_start = perf_counter()
            update_communities_posts_and_rank(db_mgr.db)
            log.info("[INIT] update_communities_posts_and_rank executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_notification_cache(cls, db):
        with AutoDbDisposer(db, "finish_notification_cache") as db_mgr:
            time_start = perf_counter()
            sql = """
                  SELECT update_notification_cache(NULL, NULL, False);
                  """
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_notification_cache executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def _finish_follow_count(cls, db, last_imported_block, current_imported_block):
        with AutoDbDisposer(db, "finish_follow_count") as db_mgr:
            time_start = perf_counter()
            sql = """
                  SELECT update_follow_count({}, {});
                  """.format(last_imported_block, current_imported_block)
            cls._execute_query(db_mgr.db, sql)
            log.info("[INIT] update_follow_count executed in %.4fs", perf_counter() - time_start)

    @classmethod
    def time_collector(cls, func, args):
        startTime = FOSM.start()
        result = func(*args)
        return FOSM.stop(startTime)

    @classmethod
    def process_tasks_in_threads(cls, info, methods):
        futures = []
        pool = ThreadPoolExecutor(max_workers=Db.max_connections)
        futures = {pool.submit(cls.time_collector, method, args): (description) for (description, method, args) in methods}

        completedThreads = 0
        for future in as_completed(futures):
          description = futures[future]
          completedThreads = completedThreads + 1
          try:
            elapsedTime = future.result()
            FOSM.final_stat(description, elapsedTime)
          except Exception as exc:
              log.error('%r generated an exception: %s' % (description, exc))
              raise exc

        pool.shutdown()
        log.info(info, completedThreads)

    @classmethod
    def _finish_all_tables(cls, massive_sync_preconditions, last_imported_block, current_imported_block):
        start_time = FOSM.start()

        log.info("#############################################################################")

        methods = []
        methods.append( ('hive_posts', cls._finish_hive_posts, [cls.db(), massive_sync_preconditions, last_imported_block, current_imported_block]) )
        methods.append( ('hive_feed_cache', cls._finish_hive_feed_cache, [cls.db(), last_imported_block, current_imported_block]) )
        methods.append( ('hive_mentions', cls._finish_hive_mentions, [cls.db(), last_imported_block, current_imported_block]) )
        methods.append( ('payout_stats_view', cls._finish_payout_stats_view, []) )
        methods.append( ('account_reputations', cls._finish_account_reputations, [cls.db(), last_imported_block, current_imported_block]) )
        methods.append( ('communities_posts_and_rank', cls._finish_communities_posts_and_rank, [cls.db()]) )
        cls.process_tasks_in_threads("[INIT] %i threads finished filling tables. Part nr 0", methods)

        methods = []
        #Notifications are dependent on many tables, therefore it's necessary to calculate it at the end
        methods.append( ('notification_cache', cls._finish_notification_cache, [cls.db()]) )
        #hive_posts_api_helper is dependent on `hive_posts/root_id` filling
        methods.append( ('hive_posts_api_helper', cls._finish_hive_posts_api_helper, [cls.db(), last_imported_block, current_imported_block]) )
        #methods `_finish_follow_count` and `_finish_account_reputations` update the same table: `hive_accounts`.
        #It can cause deadlock, therefore these functions can't be processed concurrently
        methods.append( ('follow_count', cls._finish_follow_count, [cls.db(), last_imported_block, current_imported_block]) )
        cls.process_tasks_in_threads("[INIT] %i threads finished filling tables. Part nr 1", methods)

        real_time = FOSM.stop(start_time)

        log.info("=== FILLING FINAL DATA INTO TABLES ===")
        threads_time = FOSM.log_current("Total final operations time")
        log.info(f"Elapsed time: {real_time :.4f}s. Calculated elapsed time: {threads_time :.4f}s. Difference: {real_time - threads_time :.4f}s")
        FOSM.clear()
        log.info("=== FILLING FINAL DATA INTO TABLES ===")

    @classmethod
    def _after_initial_sync(cls, current_imported_block):
        """Routine which runs *once* after initial sync.

        Re-creates non-core indexes for serving APIs after init sync,
        as well as all foreign keys."""

        start_time = perf_counter()

        last_imported_block = DbState.db().query_one("SELECT block_num FROM hive_state LIMIT 1")

        log.info("[INIT] Current imported block: %s. Last imported block: %s.", current_imported_block, last_imported_block)
        if last_imported_block > current_imported_block:
          last_imported_block = current_imported_block

        synced_blocks = current_imported_block - last_imported_block

        force_index_rebuild = False
        massive_sync_preconditions = False
        if synced_blocks >= SYNCED_BLOCK_LIMIT:
            force_index_rebuild = True
            massive_sync_preconditions = True

        #is_pre_process, drop, create
        log.info("Creating indexes: started")
        cls.processing_indexes( False, force_index_rebuild, True )
        log.info("Creating indexes: finished")

        #all post-updates are executed in different threads: one thread per one table
        log.info("Filling tables with final values: started")
        cls._finish_all_tables(massive_sync_preconditions, last_imported_block, current_imported_block)
        log.info("Filling tables with final values: finished")

        # Update a block num immediately
        cls.db().query_no_return("UPDATE hive_state SET block_num = :block_num", block_num = current_imported_block)

        if massive_sync_preconditions:
            from hive.db.schema import create_fk, set_logged_table_attribute
            # intentionally disabled since it needs a lot of WAL disk space when switching back to LOGGED
            #set_logged_table_attribute(cls.db(), True)

            log.info("Recreating foreign keys")
            create_fk(cls.db())
            log.info("Foreign keys were recreated")

            cls._execute_query(cls.db(),"VACUUM ANALYZE")

        end_time = perf_counter()
        log.info("[INIT] After initial sync actions done in %.4fs", end_time - start_time)


    @staticmethod
    def status():
        """Basic health status: head block/time, current age (secs)."""
        sql = ("SELECT num, created_at, extract(epoch from created_at) ts "
               "FROM hive_blocks ORDER BY num DESC LIMIT 1")
        row = DbState.db().query_row(sql)
        return dict(db_head_block=row['num'],
                    db_head_time=str(row['created_at']),
                    db_head_age=int(time.time() - row['ts']))

    @classmethod
    def _is_schema_loaded(cls):
        """Check if the schema has been loaded into db yet."""
        # check if database has been initialized (i.e. schema loaded)
        _engine_name = cls.db().engine_name()
        if _engine_name == 'postgresql':
            return bool(cls.db().query_one("""
                SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public'
            """))
        if _engine_name == 'mysql':
            return bool(cls.db().query_one('SHOW TABLES'))
        raise Exception("unknown db engine %s" % _engine_name)

    @classmethod
    def _is_feed_cache_empty(cls):
        """Check if the hive_feed_cache table is empty.

        If empty, it indicates that the initial sync has not finished.
        """
        return not cls.db().query_one("SELECT 1 FROM hive_feed_cache LIMIT 1")

