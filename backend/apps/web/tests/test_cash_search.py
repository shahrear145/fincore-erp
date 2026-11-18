"""নগদ (cash) page — search, sort and pagination tests.

Pinned rules:

- the search box matches the name/note and a numeric amount;
- search combines with the From–To date filter (both must hold);
- totals are computed over the whole filtered set, not just one page;
- the pager shows the right slice, keeps the other filters in its links,
  and clamps bad ``page``/``per`` values instead of erroring;
- sorting by amount reorders the rows.
"""
from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionType


class CashSearchTests(TestCase):
    def setUp(self):
        self.owner = make_user("cashsearchowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Search Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    def _add(self, name, amount, tx_date="2026-11-01"):
        self.client.post("/transactions/", {
            "amount": amount, "transaction_date": tx_date, "name": name})
        return Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.INCOME,
            description=name).first()

    def _page(self, query=""):
        res = self.client.get("/transactions/" + query)
        self.assertEqual(res.status_code, 200)
        return res.content.decode("utf-8")

    # -- search ----------------------------------------------------------
    def test_search_by_name_narrows_rows_and_total(self):
        self._add("আলু বিক্রয়", "1000.00")
        self._add("পেঁয়াজ বিক্রয়", "2000.00")

        html = self._page("?q=আলু")
        self.assertIn("আলু বিক্রয়", html)
        self.assertNotIn(">পেঁয়াজ বিক্রয়<", html)
        # only the matching row counts toward the range total
        self.assertIn("এই রেঞ্জে নগদ</span><span class=\"value\">৳1,000.00", html)
        # ...while the all-time pill still shows both rows
        self.assertIn("সব নগদ</span><span class=\"value\">৳3,000.00", html)
        self.assertIn("1</b>টি এন্ট্রি", html)

    def test_search_combines_with_the_date_filter(self):
        self._add("আলু নভেম্বর", "1000.00", "2026-11-05")
        self._add("আলু অক্টোবর", "2000.00", "2026-10-05")

        html = self._page("?q=আলু&from=2026-11-01&to=2026-11-30")
        self.assertIn("আলু নভেম্বর", html)
        self.assertNotIn(">আলু অক্টোবর<", html)
        self.assertIn("৳1,000.00", html)
        self.assertNotIn("৳2,000.00", html)

    def test_search_matches_a_numeric_amount(self):
        self._add("বড় অঙ্ক", "1234.50")
        self._add("ছোট অঙ্ক", "99.00")

        html = self._page("?q=1,234.50")
        self.assertIn("বড় অঙ্ক", html)
        self.assertNotIn(">ছোট অঙ্ক<", html)

    def test_search_without_matches_shows_the_empty_state(self):
        self._add("আলু বিক্রয়", "1000.00")
        html = self._page("?q=কিছুইনেই")
        self.assertIn("কিছুইনেই", html)
        self.assertIn("মিলে এমন কোনো নগদ এন্ট্রি নেই", html)

    # -- sorting ---------------------------------------------------------
    def test_sort_by_amount_high_and_low(self):
        self._add("ছোট", "100.00")
        self._add("বড়", "900.00")

        high = self._page("?sort=high")
        self.assertLess(high.index(">বড়<"), high.index(">ছোট<"))
        low = self._page("?sort=low")
        self.assertLess(low.index(">ছোট<"), low.index(">বড়<"))

    # -- pagination ------------------------------------------------------
    def test_pagination_slices_rows_and_counts_the_whole_set(self):
        for n in range(1, 13):          # 12 rows × 100 = 1,200
            self._add(f"এন্ট্রি {n:02d}", "100.00", "2026-11-01")

        page1 = self._page("?per=10")
        self.assertIn("এন্ট্রি 12", page1)          # newest first
        self.assertNotIn(">এন্ট্রি 02<", page1)
        self.assertIn("1–10", page1)
        self.assertIn("মোট <b>12</b>", page1)
        # the total covers all 12 rows, not just the 10 visible ones
        self.assertIn("৳1,200.00", page1)

        page2 = self._page("?per=10&page=2")
        self.assertIn(">এন্ট্রি 02<", page2)
        self.assertIn(">এন্ট্রি 01<", page2)
        self.assertNotIn("এন্ট্রি 12", page2)
        self.assertIn("11–12", page2)
        self.assertIn("৳1,200.00", page2)

    def test_pager_links_keep_the_search_and_date_filters(self):
        for n in range(1, 13):
            self._add(f"আলু {n:02d}", "100.00", "2026-11-01")

        html = self._page("?q=আলু&from=2026-11-01&to=2026-11-30&per=10")
        self.assertIn("page=2", html)
        self.assertIn("q=", html)
        self.assertIn("from=2026-11-01", html)
        self.assertIn("per=10", html)

    def test_bad_page_and_page_size_fall_back_safely(self):
        for n in range(1, 13):
            self._add(f"এন্ট্রি {n:02d}", "100.00", "2026-11-01")

        # out-of-range page → last page, not an error
        last = self._page("?per=10&page=999")
        self.assertIn(">এন্ট্রি 01<", last)
        self.assertIn("11–12", last)

        # garbage page → first page
        first = self._page("?per=10&page=abc")
        self.assertIn("1–10", first)

        # unsupported page size → 25
        fallback = self._page("?per=999")
        self.assertIn("1–12", fallback)
        self.assertIn('<option value="25" selected', fallback)
