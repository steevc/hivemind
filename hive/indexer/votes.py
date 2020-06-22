""" Votes indexing and processing """

import logging

from hive.db.adapter import Db

log = logging.getLogger(__name__)
DB = Db.instance()

class Votes:
    """ Class for managing posts votes """

    @classmethod
    def get_vote_count(cls, author, permlink):
        """ Get vote count for given post """
        sql = """
            SELECT count(hv.id) 
            FROM hive_votes hv 
            INNER JOIN hive_accounts ha_a ON ha_a.id = hv.author_id 
            INNER JOIN hive_permlink_data hpd_p ON hpd_p.id = hv.permlink_id 
            WHERE ha_a.name = :author AND hpd_p.permlink = :permlink 
        """
        ret = DB.query_row(sql, author=author, permlink=permlink)
        return 0 if ret is None else int(ret.count)

    @classmethod
    def get_upvote_count(cls, author, permlink):
        """ Get vote count for given post """
        sql = """
            SELECT count(hv.id) 
            FROM hive_votes hv 
            INNER JOIN hive_accounts ha_a ON ha_a.id = hv.author_id 
            INNER JOIN hive_permlink_data hpd_p ON hpd_p.id = hv.permlink_id 
            WHERE ha_a.name = :author AND hpd_p.permlink = :permlink
                  vote_percent > 0 
        """
        ret = DB.query_row(sql, author=author, permlink=permlink)
        return 0 if ret is None else int(ret.count)

    @classmethod
    def get_downvote_count(cls, author, permlink):
        """ Get vote count for given post """
        sql = """
            SELECT count(hv.id) 
            FROM hive_votes hv 
            INNER JOIN hive_accounts ha_a ON ha_a.id = hv.author_id 
            INNER JOIN hive_permlink_data hpd_p ON hpd_p.id = hv.permlink_id 
            WHERE ha_a.name = :author AND hpd_p.permlink = :permlink
                  vote_percent < 0 
        """
        ret = DB.query_row(sql, author=author, permlink=permlink)
        return 0 if ret is None else int(ret.count)

    @classmethod
    def vote_op(cls, vop, date):
        """ Process vote_operation """
        voter = vop['value']['voter']
        author = vop['value']['author']
        permlink = vop['value']['permlink']
        vote_percent = vop['value']['vote_percent']
        weight = vop['value']['weight']
        rshares = vop['value']['rshares']

        sql = """
            INSERT INTO hive_votes
                  (post_id, voter_id, author_id, permlink_id, weight, rshares, vote_percent, last_update) 
            SELECT hp.id, ha_v.id, ha_a.id, hpd_p.id, :weight, :rshares, :vote_percent, :last_update
            FROM hive_accounts ha_v,
                 hive_posts hp
            INNER JOIN hive_accounts ha_a ON ha_a.id = hp.author_id
            INNER JOIN hive_permlink_data hpd_p ON hpd_p.id = hp.permlink_id
            WHERE ha_a.name = :author AND hpd_p.permlink = :permlink AND ha_v.name = :voter
            ON CONFLICT ON CONSTRAINT hive_votes_ux1 DO
                UPDATE
                    SET
                        weight = EXCLUDED.weight,
                        rshares = EXCLUDED.rshares,
                        vote_percent = EXCLUDED.vote_percent,
                        last_update = EXCLUDED.last_update,
                        num_changes = hive_votes.num_changes + 1
                WHERE hive_votes.id = EXCLUDED.id
        """
        DB.query(sql, voter=voter, author=author, permlink=permlink, weight=weight, rshares=rshares,
                 vote_percent=vote_percent, last_update=date)
