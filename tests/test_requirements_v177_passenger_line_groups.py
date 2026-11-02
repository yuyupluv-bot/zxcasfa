"""V177: passenger-only free-line groups, at the bottom without headings."""
from __future__ import annotations
import ast
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load, node, source

class PassengerLineGroupsV177(unittest.TestCase):
    def renderer(self):
        scope={'format_rating':lambda d:'⭐ 5.0 (7 отзывов)',
               '_DISPLAY_ORIGIN_LABELS':{'Пашия':'из Пашии'},
               '_DISPLAY_DESTINATION_LABELS':{'Чусовой':'до Чусового'},
               '_display_route_direction':lambda o:(None,'Чусовой') if o else (None,None)}
        load('bot/handlers.py','_render_driver_statuses',scope)
        return load('bot/handlers.py','_render_passenger_driver_statuses',scope),scope

    def item(self,id,line,status='online',position=None):
        return {'driver':NS(id=id,vk_id=100+id,full_name=f'Водитель {id}',current_line=line),
                'status':status,'position':id if position is None else position,
                'order':NS() if status=='busy' else None}

    def sample(self):
        return [self.item(1,'Пашия'),self.item(2,'Горнозаводск'),self.item(3,'Кусья'),
                self.item(4,'Пашия'),self.item(5,'Чусовой','busy'),self.item(6,'Кусья','away'),
                self.item(7,'Горнозаводск','considering'),self.item(8,'Горнозаводск')]

    def test_other_lines_below_all_other_statuses(self):
        render,_=self.renderer();text='\n'.join(render(self.sample()))
        self.assertLess(text.index('🟢 Водитель 2'),text.index('🟠 Водитель 7'))
        self.assertLess(text.index('🟠 Водитель 7'),text.index('🟡 Водитель 6'))
        self.assertLess(text.index('🟡 Водитель 6'),text.index('🔴 Водитель 5'))
        self.assertLess(text.index('🔴 Водитель 5'),text.index('🟢 Водитель 1'))
        self.assertLess(text.index('🟢 Водитель 1'),text.index('🟢 Водитель 3'))

    def test_same_line_is_contiguous_other_lines_separated_by_one_blank(self):
        render,_=self.renderer();lines=render(self.sample())
        first=next(i for i,line in enumerate(lines) if '🟢 Водитель 1 ' in line)
        self.assertIn('🟢 Водитель 4 ',lines[first+1])
        self.assertEqual(lines[first-1],'')
        self.assertEqual(lines[first+2],'')
        self.assertIn('🟢 Водитель 3 ',lines[first+3])

    def test_no_free_line_headings_or_numbering(self):
        render,_=self.renderer();text='\n'.join(render(self.sample()))
        self.assertNotIn('Свободны',text);self.assertNotIn('Другие статусы:',text)
        self.assertNotRegex(text,r'(?m)^\d+\. ')
        self.assertIn('свободен — Пашия — ⭐ 5.0 (7 отзывов)',text)
        self.assertIn('свободен — Кусья — ⭐ 5.0 (7 отзывов)',text)

    def test_empty_and_single_line_have_no_edge_blank_lines(self):
        render,_=self.renderer()
        self.assertEqual(render([]),[])
        for items in ([self.item(1,'Пашия')],[self.item(1,'Горнозаводск')]):
            with self.subTest(line=items[0]['driver'].current_line):
                lines=render(items)
                self.assertEqual(len(lines),1);self.assertNotEqual(lines[0],'')

    def test_only_other_lines_have_exactly_one_blank_between_groups(self):
        render,_=self.renderer();lines=render([self.item(1,'Кусья'),self.item(2,'Пашия'),self.item(3,'Сараны')])
        self.assertEqual(len(lines),5)
        self.assertIn('Пашия',lines[0]);self.assertEqual(lines[1],'')
        self.assertIn('Кусья',lines[2]);self.assertEqual(lines[3],'')
        self.assertIn('Сараны',lines[4])

    def test_case_and_outer_spaces_do_not_split_same_line(self):
        render,_=self.renderer();lines=render([self.item(1,' Пашия '),self.item(2,'ГОРНОЗАВОДСК'),self.item(3,'пашия')])
        self.assertEqual(len(lines),4)
        self.assertIn('Водитель 2',lines[0]);self.assertEqual(lines[1],'')
        self.assertIn('Водитель 1',lines[2]);self.assertIn('Водитель 3',lines[3])

    def test_unknown_line_not_guessed_and_custom_lines_grouped(self):
        render,_=self.renderer();lines=render([self.item(1,None),self.item(2,'Койва'),self.item(3,'Койва'),self.item(4,'Дачи')])
        self.assertIn('Локация не указана',lines[0])
        text='\n'.join(lines)
        self.assertIn('свободен — Койва',text);self.assertIn('свободен — Дачи',text)
        self.assertEqual(text.count('Водитель '),4)

    def test_driver_fifo_order_inside_line_is_retained(self):
        render,_=self.renderer();lines=render([self.item(1,'Пашия',position=5),self.item(2,'Пашия',position=2)])
        self.assertIn('Водитель 2',lines[0]);self.assertIn('Водитель 1',lines[1])

    def test_input_data_and_order_are_not_mutated(self):
        render,_=self.renderer();items=self.sample();before=[dict(item) for item in items]
        render(items);self.assertEqual(items,before)

    def test_main_route_sections_and_ratings_are_preserved(self):
        render,_=self.renderer();text='\n'.join(render(self.sample()))
        self.assertIn('На заявке до Чусового:',text)
        self.assertIn('🔴 Водитель 5 — на заявке — до Чусового — ⭐ 5.0 (7 отзывов)',text)
        self.assertEqual(text.count('⭐ 5.0 (7 отзывов)'),len(self.sample()))

    def test_driver_and_dispatcher_queue_remain_numbered_without_free_headings(self):
        _,scope=self.renderer();lines=scope['_render_driver_statuses'](self.sample(),numbered=True,group_free_by_line=True)
        text='\n'.join(lines)
        self.assertNotIn('Свободны —',text)
        self.assertIn('свободен — Горнозаводск',text)
        self.assertIn('свободен — Пашия',text)
        self.assertIn('1. 🟢 Водитель 1',text)
        screen=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','show_queue'))
        self.assertNotIn('_render_passenger_driver_statuses',screen)
        self.assertIn('group_free_by_line=True',screen)

    def test_both_passenger_list_paths_use_wrapper_without_changing_keyboard(self):
        for name in ('_show_driver_status_overview','show_free_drivers'):
            with self.subTest(function=name):
                text=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py',name))
                self.assertIn('_render_passenger_driver_statuses(items)',text)
                self.assertIn('selectable_ids',text)
                self.assertIn('"order": active_orders.get(driver.id)',text)
        active=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','_show_driver_status_overview'))
        self.assertIn('kb.passenger_ride_keyboard(active.id)',active)
        self.assertIn('kb.passenger_arrived_keyboard(active.id)',active)
        main=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','show_free_drivers'))
        self.assertIn('has_booking=has_booking',main)

    def test_new_presentation_does_not_query_or_dispatch(self):
        wrapper=ast.get_source_segment(source('bot/handlers.py'),node('bot/handlers.py','_render_passenger_driver_statuses'))
        for forbidden in ('session.','queue_service.','order_service.','offer_to_next_driver','set_state('):
            self.assertNotIn(forbidden,wrapper)

if __name__=='__main__':unittest.main()
