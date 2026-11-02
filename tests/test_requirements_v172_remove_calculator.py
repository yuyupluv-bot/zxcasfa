"""Removal of approximate fares and recovery of legacy controls."""
import ast
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[1]

class CalculatorRemovalV172(unittest.TestCase):
    def test_price_menus_have_no_calculator(self):
        from bot import keyboards
        for active in (False, True):
            data = json.loads(keyboards.price_menu_keyboard(
                [('a', 'Раздел A'), ('b', 'Раздел B')],
                {'btn_price_calculate': 'Старая настройка'}, active_order=active))
            commands = [b['action']['payload'] for r in data['buttons'] for b in r]
            self.assertFalse(any('price_calculate' in c for c in commands))
            self.assertEqual(len(data['buttons']), 2)
            self.assertIn('price_section', commands[0])
            self.assertIn('price_back', commands[-1])

    def test_calculation_code_and_defaults_removed(self):
        self.assertFalse((ROOT / 'bot/price_calculator.py').exists())
        source = (ROOT / 'bot/handlers.py').read_text()
        for text in ('price_calculator.estimate', 'def price_calculate_route(', 'def price_calculate_start(', 'Примерная стоимость:'):
            self.assertNotIn(text, source)
        settings = (ROOT / 'common/settings_service.py').read_text()
        self.assertNotIn('price_calc_per_km', settings)
        self.assertNotIn('btn_price_calculate', settings)
        self.assertNotIn('P_PRICE_CALC_ROUTE', (ROOT / 'bot/states_service.py').read_text())

    def test_legacy_routes_redirect_for_all_roles(self):
        source = (ROOT / 'bot/handlers.py').read_text()
        self.assertEqual(source.count('if cmd == "price_calculate":\n        return retired_price_calculator(session, user)'), 3)
        self.assertEqual(source.count('if state == "passenger_price_calc_route":\n        return retired_price_calculator(session, user)'), 3)

    def recovery(self, state):
        tree = ast.parse((ROOT / 'bot/handlers.py').read_text())
        f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'retired_price_calculator')
        scope = {'Session': object, 'User': object, 'get_state': Mock(return_value=SimpleNamespace(state=state)),
                 '_reset_price_state': Mock(), 'show_price': Mock()}
        exec(compile(ast.Module(body=[f], type_ignores=[]), '<recovery>', 'exec'), scope)
        session, user = object(), SimpleNamespace(vk_id=123)
        scope['retired_price_calculator'](session, user)
        scope['show_price'].assert_called_once_with(session, user)
        return scope['_reset_price_state'], session, user

    def test_legacy_state_is_reset(self):
        reset, session, user = self.recovery('passenger_price_calc_route')
        reset.assert_called_once_with(session, user)

    def test_old_button_preserves_active_trip_state(self):
        reset, _, _ = self.recovery('driver_finish_price')
        reset.assert_not_called()
