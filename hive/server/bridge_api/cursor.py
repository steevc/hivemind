"""Cursor-based pagination queries, mostly supporting bridge_api."""

from hive.server.common.helpers import last_month

from hive.server.hive_api.common import (get_account_id, get_post_id)

# pylint: disable=too-many-lines

async def _pinned(db, community_id):
    """Get a list of pinned post `id`s in `community`."""
    sql = """SELECT id FROM hive_posts
              WHERE is_pinned = '1'
                AND counter_deleted = 0
                AND community_id = :community_id
            ORDER BY id DESC"""
    return await db.query_col(sql, community_id=community_id)


async def pids_by_blog(db, account: str, start_author: str = '',
                       start_permlink: str = '', limit: int = 20):
    """Get a list of post_ids for an author's blog."""
    account_id = await get_account_id(db, account)

    seek = ''
    start_id = None
    if start_permlink:
        start_id = await get_post_id(db, start_author, start_permlink)
        seek = """
          AND created_at <= (
            SELECT created_at
              FROM hive_feed_cache
             WHERE account_id = :account_id
               AND post_id = :start_id)
        """

    # ignore community posts which were not reblogged
    skip = """
        SELECT
            hp.id
        FROM
            hive_posts hp
        INNER JOIN hive_accounts ha_hp ON ha_hp.id = hp.author_id
        WHERE
            ha_hp.name = :account
            AND hp.counter_deleted = 0
            AND hp.depth = 0
            AND hp.community_id IS NOT NULL
            AND hp.id NOT IN (
                SELECT
                    hr.post_id
                FROM
                    hive_reblogs hr
                INNER JOIN hive_accounts ha_hr ON ha_hr.id = hr.blogger_id
                WHERE ha_hr.name = :account
            )
    """

    sql = """
        SELECT post_id
          FROM hive_feed_cache
         WHERE account_id = :account_id %s
           AND post_id NOT IN (%s)
      ORDER BY created_at DESC
         LIMIT :limit
    """ % (seek, skip)

    # alternate implementation -- may be more efficient
    #sql = """
    #    SELECT id
    #      FROM (
    #             SELECT id, author account, created_at FROM hive_posts
    #              WHERE depth = 0 AND counter_deleted = 0 AND community_id IS NULL
    #              UNION ALL
    #             SELECT post_id id, account, created_at FROM hive_reblogs
    #           ) blog
    #     WHERE account = :account %s
    #  ORDER BY created_at DESC
    #     LIMIT :limit
    #""" % seek

    return await db.query_col(sql, account_id=account_id, account=account,
                              start_id=start_id, limit=limit)

async def pids_by_feed_with_reblog(db, account: str, start_author: str = '',
                                   start_permlink: str = '', limit: int = 20):
    """Get a list of [post_id, reblogged_by_str] for an account's feed."""
    account_id = await get_account_id(db, account)

    seek = ''
    start_id = None
    if start_permlink:
        start_id = await get_post_id(db, start_author, start_permlink)
        if not start_id:
            return []

        seek = """
          HAVING MIN(hive_feed_cache.created_at) <= (
            SELECT MIN(created_at) FROM hive_feed_cache WHERE post_id = :start_id
               AND account_id IN (SELECT following FROM hive_follows
                                  WHERE follower = :account AND state = 1))
        """

    sql = """
        SELECT post_id, string_agg(name, ',') accounts
          FROM hive_feed_cache
          JOIN hive_follows ON account_id = hive_follows.following AND state = 1
          JOIN hive_accounts ON hive_follows.following = hive_accounts.id
         WHERE hive_follows.follower = :account
           AND hive_feed_cache.created_at > :cutoff
      GROUP BY post_id %s
      ORDER BY MIN(hive_feed_cache.created_at) DESC LIMIT :limit
    """ % seek

    result = await db.query_all(sql, account=account_id, start_id=start_id,
                                limit=limit, cutoff=last_month())
    return [(row[0], row[1]) for row in result]

async def pids_by_comments(db, account: str, start_permlink: str = '', limit: int = 20):
    """Get a list of post_ids representing comments by an author."""
    seek = ''
    start_id = None
    if start_permlink:
        start_id = await get_post_id(db, account, start_permlink)
        if not start_id:
            return []

        seek = "AND id <= :start_id"

    # `depth` in ORDER BY is a no-op, but forces an ix3 index scan (see #189)
    sql = """
        SELECT id FROM hive_posts
         WHERE author = (SELECT id FROM hive_accounts WHERE name = :account) %s
           AND counter_deleted = 0
           AND depth > 0
      ORDER BY id DESC, depth
         LIMIT :limit
    """ % seek

    return await db.query_col(sql, account=account, start_id=start_id, limit=limit)


async def pids_by_replies(db, author: str, start_replies_author: str, start_replies_permlink: str = '',
                          limit: int = 20):
    """Get a list of post_ids representing replies to an author.
    """

    sql = "SELECT get_account_post_replies( (:author)::VARCHAR, (:start_author)::VARCHAR, (:start_permlink)::VARCHAR, (:limit)::SMALLINT ) as id"
    return await db.query_col(
        sql, author=author, start_author=start_replies_author, start_permlink=start_replies_permlink, limit=limit)
