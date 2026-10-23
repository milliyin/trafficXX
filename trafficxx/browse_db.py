"""Browse pool policy; preserve the existing import used by diagnostics."""
from trafficxx.database_pool import DatabasePool


class BrowseDatabase(DatabasePool):
    def __init__(self):
        super().__init__('browse', 'BROWSE_DB_POOL_MAX')
