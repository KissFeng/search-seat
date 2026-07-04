import unittest

import app


class AdminPaginationTests(unittest.TestCase):
    def test_default_pagination_is_first_page_with_ten_items(self):
        pagination = app.admin_pagination({})

        self.assertEqual(pagination, {"page": 1, "page_size": 10, "offset": 0})

    def test_pagination_parses_query_values(self):
        pagination = app.admin_pagination({"page": ["3"], "page_size": ["5"]})

        self.assertEqual(pagination, {"page": 3, "page_size": 5, "offset": 10})

    def test_pagination_clamps_invalid_values(self):
        pagination = app.admin_pagination({"page": ["0"], "page_size": ["500"]})

        self.assertEqual(pagination, {"page": 1, "page_size": 50, "offset": 0})


if __name__ == "__main__":
    unittest.main()
