"""Admin giveaway: explicit selection, eligibility and random fallback."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-key")
os.environ["AUTO_MIGRATE_ON_START"] = "0"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from common.models import Base, User, Setting, AdminLog
from web.giveaway_selection import parse_winner_ids, select_winners
import web.app as web


class SelectionTests(unittest.TestCase):
    def test_parser_and_validation(self):
        self.assertEqual(parse_winner_ids("123\nid456\nhttps://vk.com/id789/"), [123, 456, 789])
        for value in ("vk.com/name", "0", "123\nid123", "https://evil.com/id123", "9223372036854775808", "\n".join(map(str, range(1, 52)))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_winner_ids(value)

    def test_selection_and_random_fallback(self):
        candidates = [{"vk_id": i} for i in (1, 2, 3)]
        with patch("web.giveaway_selection.secrets.SystemRandom") as random:
            random.return_value.sample.side_effect = lambda pool, n: pool[:n]
            self.assertEqual([x["vk_id"] for x in select_winners(candidates, [2], 2)], [2, 1])
            random.return_value.sample.assert_called_with([candidates[0], candidates[2]], 1)
            self.assertEqual([x["vk_id"] for x in select_winners(candidates, [], 2)], [1, 2])
            random.return_value.sample.assert_called_with(candidates, 2)
        for fixed, count in (([4], 1), ([1, 2], 1), ([], 4)):
            with self.subTest(fixed=fixed), self.assertRaises(ValueError):
                select_winners(candidates, fixed, count)


class AdminRouteTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(bind=engine, expire_on_commit=False)
        with self.factory.begin() as s:
            s.add_all([User(vk_id=101, full_name="One"), User(vk_id=202, full_name="Two"), User(vk_id=303, full_name="Blocked", is_blocked=True)])
        self.patch = patch.object(web, "get_session", self.factory)
        self.patch.start()
        web.app.config.update(TESTING=True, SECRET_KEY="test-key")
        self.client = web.app.test_client()
        with self.client.session_transaction() as sess:
            sess["admin_id"] = 1
            sess["csrf_token"] = "token"

    def tearDown(self):
        self.patch.stop()

    def save(self, text):
        return self.client.post("/giveaway/winners", data={"csrf_token": "token", "winner_ids": text}, follow_redirects=True)

    def draw(self, count=2, **kwargs):
        data = {"csrf_token": "token", "action": "draw", "winners_count": str(count), "contest_text": "Тест", "prizes": "Приз", **kwargs}
        return self.client.post("/giveaway", data=data)

    def test_save_draw_repeat_and_clear(self):
        response = self.save("https://vk.com/id202")
        self.assertIn("Список победителей сохранён", response.get_data(as_text=True))
        for _ in range(2):
            content = self.draw().get_data(as_text=True)
            self.assertIn("Первое место назначено организатором", content)
            self.assertIn("[id202|Two]", content)
            self.assertIn("[id101|One]", content)
            self.assertLess(content.index("[id202|Two]"), content.index("[id101|One]"))
        self.save("")
        content = self.draw().get_data(as_text=True)
        self.assertNotIn("назначено организатором", content)
        with self.factory() as s:
            self.assertEqual(s.query(Setting).filter_by(key=web.GIVEAWAY_FIXED_WINNERS_KEY).one().value, "")
            self.assertEqual(s.query(AdminLog).filter_by(action="giveaway_draw").count(), 3)

    def test_bad_list_ineligible_and_count(self):
        self.save("202")
        self.assertIn("не найдены", self.save("999").get_data(as_text=True))
        self.assertIn("202", self.client.get("/giveaway/winners").get_data(as_text=True))
        self.assertIn("не подходят", self.draw(condition_order="1").get_data(as_text=True))
        self.save("202\n101")
        self.assertIn("больше, чем указано мест", self.draw(count=1).get_data(as_text=True))
        self.save("303")
        self.assertIn("не подходят", self.draw(count=1).get_data(as_text=True))

    def test_auth_and_csrf(self):
        self.assertEqual(self.client.post("/giveaway/winners", data={"winner_ids": "101"}).status_code, 400)
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get("/giveaway/winners").status_code, 302)


if __name__ == "__main__":
    unittest.main()
