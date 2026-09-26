import unittest

from app.store import NotFound, Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()

    def test_add_and_get(self) -> None:
        item = self.store.add("widget")
        self.assertEqual(item["name"], "widget")
        self.assertEqual(self.store.get(item["id"]), item)

    def test_list_is_sorted_by_id(self) -> None:
        first = self.store.add("a")
        second = self.store.add("b")
        self.assertEqual([i["id"] for i in self.store.list()], [first["id"], second["id"]])

    def test_get_missing_raises_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.store.get(999)

    def test_add_rejects_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add("   ")

    def test_delete(self) -> None:
        item = self.store.add("gone")
        self.store.delete(item["id"])
        with self.assertRaises(NotFound):
            self.store.get(item["id"])

    def test_delete_missing_raises_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.store.delete(123)


if __name__ == "__main__":
    unittest.main()
