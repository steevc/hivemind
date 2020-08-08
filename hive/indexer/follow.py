"""Handles follow operations."""

import logging
from time import perf_counter as perf

from funcy.seqs import first
from hive.db.adapter import Db
from hive.db.db_state import DbState
from hive.indexer.accounts import Accounts
from hive.indexer.notify import Notify

log = logging.getLogger(__name__)

DB = Db.instance()

FOLLOWERS = 'followers'
FOLLOWING = 'following'

FOLLOW_ITEM_INSERT_QUERY = """
    INSERT INTO hive_follows as hf (follower, following, created_at, state, blacklisted, follow_blacklists, follow_muted)
    VALUES
        (
            :flr,
            :flg,
            :at,
            :state,
            (CASE :state
                WHEN 3 THEN TRUE
                WHEN 5 THEN FALSE
                ELSE FALSE
            END
            ),
            (CASE :state
                WHEN 4 THEN FALSE
                WHEN 6 THEN TRUE
                ELSE FALSE
            END
            ),
            (CASE :state
                WHEN 7 THEN TRUE
                WHEN 8 THEN FALSE
                ELSE FALSE
            END
            )
        )
    ON CONFLICT (follower, following) DO UPDATE
        SET
            state = (CASE EXCLUDED.state
                        WHEN 0 THEN 0 -- 0 blocks possibility to update state
                        ELSE EXCLUDED.state
                     END),
            blacklisted = (CASE EXCLUDED.state
                              WHEN 3 THEN TRUE
                              WHEN 5 THEN FALSE
                              ELSE hf.blacklisted
                          END),
            follow_blacklists = (CASE EXCLUDED.state
                                    WHEN 4 THEN TRUE
                                    WHEN 6 THEN FALSE
                                    ELSE hf.follow_blacklists
                                END),
            follow_muted = (CASE EXCLUDED.state
                                WHEN 7 THEN TRUE
                                WHEN 8 THEN FALSE
                                ELSE hf.follow_muted
                            END)
    """

def _flip_dict(dict_to_flip):
    """Swap keys/values. Returned dict values are array of keys."""
    flipped = {}
    for key, value in dict_to_flip.items():
        if value in flipped:
            flipped[value].append(key)
        else:
            flipped[value] = [key]
    return flipped

class Follow:
    """Handles processing of incoming follow ups and flushing to db."""

    follow_items_to_flush = dict()

    @classmethod
    def follow_op(cls, account, op_json, date):
        """Process an incoming follow op."""
        op = cls._validated_op(account, op_json, date)
        if not op:
            return

        # perform delta check
        new_state = op['state']
        old_state = None
        if DbState.is_initial_sync():
            # insert or update state

            k = '{}/{}'.format(op['flr'], op['flg'])

            if k in cls.follow_items_to_flush:
                old_value = cls.follow_items_to_flush.get(k)
                old_value['state'] = op['state']
                cls.follow_items_to_flush[k] = old_value
            else:
                cls.follow_items_to_flush[k] = dict(
                                                      flr=op['flr'],
                                                      flg=op['flg'],
                                                      state=op['state'],
                                                      at=op['at'])

        else:
            if isinstance(op['flg'], list):
                print('multi_follow, op is: ', op)
                for following_id in op['flg']:
                    following_id = following_id[0]
                    DB.query(FOLLOW_ITEM_INSERT_QUERY, flr=op['flr'], flg=following_id, at=op['at'], state=op['state'])
                return

            state = op['state']
            if state > 8:
                sql = ''
                sql2 = ''
                if state == 9:
                    #reset blacklists for follower
                    sql = """UPDATE hive_follows set blacklisted = false where follower = :follower"""
                elif state == 10:
                    #reset following list for follower
                    sql = """UPDATE hive_follows set state = 0 where follower = :follower AND state = 1"""
                elif state == 11:
                    #reset all muted list for follower
                    sql = """UPDATE hive_follows set state = 0 where follower = :follower AND state = 2"""
                elif state == 12:
                    #reset followed blacklists
                    sql = """UPDATE hive_follows set follow_blacklists = false where follower = :follower"""
                    sql2 = """UPDATE hive_follows SET follow_blacklists = true WHERE follower = :follower AND following = (SELECT id FROM hive_accounts WHERE name = 'null')"""
                elif state == 13:
                    #reset followed mute lists
                    sql = """UPDATE hive_follows set follow_muted = false where follower = :follower"""
                    sql2 = """UPDATE hive_follows SET follow_muted = true WHERE follower = :follower AND following = (SELECT id FROM hive_accounts WHERE name = 'null')"""
                elif state == 14:
                    #reset all lists
                    sql = """UPDATE hive_follows set blacklisted = false, follow_blacklists = false, follow_muted = false, state = 0 where follower = :follower"""
                    sql2 = """UPDATE hive_follows SET follow_blacklists = true, follow_muted = true WHERE follower = :follower AND following = (SELECT id FROM hive_accounts WHERE name = 'null')"""

                DB.query(sql, follower=op['flr'])
                if sql2 != '':
                    DB.query(sql2, follower=op['flr'])
                return

            old_state = cls._get_follow_db_state(op['flr'], op['flg'])
            # insert or update state


            DB.query(FOLLOW_ITEM_INSERT_QUERY, **op)
            if new_state == 1:
                Follow.follow(op['flr'], op['flg'])
                if old_state is None:
                    score = Accounts.default_score(op_json['follower'])
                    Notify('follow', src_id=op['flr'], dst_id=op['flg'],
                           when=op['at'], score=score).write()
            if old_state == 1:
                Follow.unfollow(op['flr'], op['flg'])

    @classmethod
    def _validated_op(cls, account, op, date):
        """Validate and normalize the operation."""
        if(not 'what' in op
           or not isinstance(op['what'], list)
           or not 'follower' in op
           or not 'following' in op):
            return None

        what = first(op['what']) or ''
        if not isinstance(what, str):
            return None
        defs = {'': 0, 'blog': 1, 'ignore': 2, 'blacklist': 3, 'follow_blacklist': 4, 'unblacklist': 5, 'unfollow_blacklist': 6,
                'follow_muted': 7, 'unfollow_muted': 8, 'reset_blacklist': 9, 'reset_following_list': 10, 'reset_muted_list': 11, 'reset_follow_blacklist': 12,
                'reset_follow_muted_list': 13, 'reset_all_lists': 14}
        if what not in defs:
            return None

        if isinstance(op['following'], list):
            all_accounts = list(op['following'])
            all_accounts.append(op['follower'])
            if (op['follower'] in op['following']
            or op['follower'] != account
            or not cls._are_accounts_valid(all_accounts)):
                return None

        else:
            if(op['follower'] == op['following']        # can't follow self
            or op['follower'] != account             # impersonation
            or not Accounts.exists(op['following'])  # invalid account
            or not Accounts.exists(op['follower'])): # invalid account
                return None

        return dict(flr=Accounts.get_id(op['follower']),
                    flg=Accounts.get_id(op['following']) if not isinstance(op['following'], list) else cls._get_ids_for_accounts(op['following']),
                    state=defs[what],
                    at=date)

    @classmethod
    def _are_accounts_valid(cls, accounts):
        if not isinstance(accounts, list):
            return False
        sql = "select count(*) as total from hive_accounts where name in :names"
        sql_result = DB.query_all(sql, names=tuple(accounts))
        names_found = sql_result[0]['total']
        if names_found != len(accounts):
            return False
        return True

    @classmethod
    def _get_ids_for_accounts(cls, accounts):
        sql = "select id from hive_accounts where name in :names"
        sql_result = DB.query_all(sql, names=tuple(accounts))
        results = []
        for row in sql_result:
            results.append(row)
        return results

    @classmethod
    def _get_follow_db_state(cls, follower, following):
        """Retrieve current follow state of an account pair."""
        sql = """SELECT state FROM hive_follows
                  WHERE follower = :follower
                    AND following = :following"""
        return DB.query_one(sql, follower=follower, following=following)


    # -- stat tracking --

    _delta = {FOLLOWERS: {}, FOLLOWING: {}}

    @classmethod
    def follow(cls, follower, following):
        """Applies follow count change the next flush."""
        cls._apply_delta(follower, FOLLOWING, 1)
        cls._apply_delta(following, FOLLOWERS, 1)

    @classmethod
    def unfollow(cls, follower, following):
        """Applies follow count change the next flush."""
        cls._apply_delta(follower, FOLLOWING, -1)
        cls._apply_delta(following, FOLLOWERS, -1)

    @classmethod
    def _apply_delta(cls, account, role, direction):
        """Modify an account's follow delta in specified direction."""
        if not account in cls._delta[role]:
            cls._delta[role][account] = 0
        cls._delta[role][account] += direction

    @classmethod
    def _flush_follow_items(cls):
        sql_prefix = """
              INSERT INTO hive_follows as hf (follower, following, created_at, state, blacklisted, follow_blacklists)
              VALUES """

        sql_postfix = """
              ON CONFLICT ON CONSTRAINT hive_follows_pk DO UPDATE
                SET
                    state = (CASE EXCLUDED.state
                                WHEN 0 THEN 0 -- 0 blocks possibility to update state
                                ELSE EXCLUDED.state
                            END),
                    blacklisted = (CASE EXCLUDED.state
                                    WHEN 3 THEN TRUE
                                    WHEN 5 THEN FALSE
                                    ELSE EXCLUDED.blacklisted
                                END),
                    follow_blacklists = (CASE EXCLUDED.state
                                            WHEN 4 THEN TRUE
                                            WHEN 6 THEN FALSE
                                            ELSE EXCLUDED.follow_blacklists
                                        END)
              WHERE hf.following = EXCLUDED.following AND hf.follower = EXCLUDED.follower
              """
        values = []
        limit = 1000
        count = 0
        for _, follow_item in cls.follow_items_to_flush.items():
            if count < limit:
                values.append("({}, {}, '{}', {}, {}, {})".format(follow_item['flr'], follow_item['flg'],
                                                                  follow_item['at'], follow_item['state'],
                                                                  follow_item['state'] == 3,
                                                                  follow_item['state'] == 4))
                count = count + 1
            else:
                query = sql_prefix + ",".join(values)
                query += sql_postfix
                DB.query(query)
                values.clear()
                values.append("({}, {}, '{}', {}, {}, {})".format(follow_item['flr'], follow_item['flg'],
                                                                  follow_item['at'], follow_item['state'],
                                                                  follow_item['state'] == 3,
                                                                  follow_item['state'] == 4))
                count = 1

        if len(values) > 0:
            query = sql_prefix + ",".join(values)
            query += sql_postfix
            DB.query(query)

        cls.follow_items_to_flush.clear()

    @classmethod
    def flush(cls, trx=True):
        """Flushes pending follow count deltas."""

        cls._flush_follow_items()

        updated = 0
        sqls = []
        for col, deltas in cls._delta.items():
            for delta, names in _flip_dict(deltas).items():
                updated += len(names)
                sql = "UPDATE hive_accounts SET %s = %s + :mag WHERE id IN :ids"
                sqls.append((sql % (col, col), dict(mag=delta, ids=tuple(names))))

        if not updated:
            return 0

        start = perf()
        DB.batch_queries(sqls, trx=trx)
        if trx:
            log.info("[SYNC] flushed %d follow deltas in %ds",
                     updated, perf() - start)

        cls._delta = {FOLLOWERS: {}, FOLLOWING: {}}
        return updated

    @classmethod
    def flush_recount(cls):
        """Recounts follows/following counts for all queued accounts.

        This is currently not used; this approach was shown to be too
        expensive, but it's useful in case follow counts manage to get
        out of sync.
        """
        ids = set([*cls._delta[FOLLOWERS].keys(),
                   *cls._delta[FOLLOWING].keys()])
        sql = """
            UPDATE hive_accounts
               SET followers = (SELECT COUNT(*) FROM hive_follows WHERE state = 1 AND following = hive_accounts.id),
                   following = (SELECT COUNT(*) FROM hive_follows WHERE state = 1 AND follower  = hive_accounts.id)
             WHERE id IN :ids
        """
        DB.query(sql, ids=tuple(ids))

    @classmethod
    def force_recount(cls):
        """Recounts all follows after init sync."""
        log.info("[SYNC] query follower counts")
        sql = """
            CREATE TEMPORARY TABLE following_counts AS (
                  SELECT id account_id, COUNT(state) num
                    FROM hive_accounts
               LEFT JOIN hive_follows hf ON id = hf.follower AND state = 1
                GROUP BY id);
            CREATE TEMPORARY TABLE follower_counts AS (
                  SELECT id account_id, COUNT(state) num
                    FROM hive_accounts
               LEFT JOIN hive_follows hf ON id = hf.following AND state = 1
                GROUP BY id);
        """
        DB.query(sql)

        log.info("[SYNC] update follower counts")
        sql = """
            UPDATE hive_accounts SET followers = num FROM follower_counts
             WHERE id = account_id AND followers != num;

            UPDATE hive_accounts SET following = num FROM following_counts
             WHERE id = account_id AND following != num;
        """
        DB.query(sql)
