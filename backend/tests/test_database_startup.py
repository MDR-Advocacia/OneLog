import unittest
from unittest import mock

import database


class DatabaseStartupTests(unittest.TestCase):
    def test_current_schema_does_not_open_a_ddl_transaction(self):
        inspector = mock.Mock()
        inspector.get_columns.return_value = [
            {"name": name} for name in database.ACCOUNT_COLUMN_MIGRATIONS
        ]

        with (
            mock.patch.object(database.Base.metadata, "create_all") as create_all,
            mock.patch.object(database, "inspect", return_value=inspector),
            mock.patch.object(database.engine, "begin") as begin,
        ):
            database.init_db()

        create_all.assert_called_once_with(bind=database.engine)
        begin.assert_not_called()

    def test_only_missing_columns_are_migrated(self):
        missing_column = "titular"
        inspector = mock.Mock()
        inspector.get_columns.return_value = [
            {"name": name}
            for name in database.ACCOUNT_COLUMN_MIGRATIONS
            if name != missing_column
        ]
        connection = mock.Mock()
        transaction = mock.MagicMock()
        transaction.__enter__.return_value = connection

        with (
            mock.patch.object(database.Base.metadata, "create_all"),
            mock.patch.object(database, "inspect", return_value=inspector),
            mock.patch.object(database.engine, "begin", return_value=transaction),
        ):
            database.init_db()

        connection.execute.assert_called_once()
        statement = str(connection.execute.call_args.args[0])
        self.assertIn("ADD COLUMN titular", statement)


if __name__ == "__main__":
    unittest.main()
