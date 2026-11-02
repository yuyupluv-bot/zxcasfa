"""V178: driver/dispatcher queue without queue/free-line heading rows."""
from __future__ import annotations
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
from test_requirements_v173_requested_changes import load

class QueueWithoutHeadingsV178(unittest.TestCase):
    def scope(self):
        scope={'format_rating':lambda d:'⭐ 5.0 (7 отзывов)',
               '_DISPLAY_ORIGIN_LABELS':{},'_DISPLAY_DESTINATION_LABELS':{'Чусовой':'до Чусового'},
               '_display_route_direction':lambda o:(None,'Чусовой') if o else (None,None)}
        load('bot/handlers.py','_render_driver_statuses',scope)
        return scope

    def fixture(self,dispatcher=False,empty=False,offline=False,considering=False):
        users=[NS(id=id,vk_id=100+id,full_name=f'Водитель {id}',current_line='НЕВЕРНАЯ ЛИНИЯ',driver_status='online')
               for id in (1,2,3)]
        entries=[] if empty else [dict(driver=d,status='waiting',live_status='online',city_id=1 if d.id<3 else 2,position=d.id)
                                  for d in users]
        if offline:
            for e in entries:e['live_status']='offline'
        if considering:
            entries[0].update(status='offered',live_status='considering')
        scope=self.scope()
        scope.update({'queue_service':NS(_notify_fronts=Mock(),queue_entries=lambda s:entries,
                                        driver_line_rank=lambda *a,**kw:None if empty or offline or considering else 1),
                      'City':object,'_active_orders_by_driver':lambda *a:{},
                      '_dispatcher_passenger_details':lambda *a:{},'can_switch_role':lambda u:False,
                      'kb':Mock(),'vk':Mock()})
        session=Mock();session.query.return_value.all.return_value=[NS(id=1,name='Горнозаводск'),NS(id=2,name='Пашия')]
        load('bot/handlers.py','show_queue',scope)(session,users[0],dispatcher_view=dispatcher)
        return scope,scope['vk'].send_message.call_args.args[1]

    def test_driver_queue_has_no_queue_or_free_line_headings(self):
        _,text=self.fixture()
        self.assertNotIn('Очередь водителей',text);self.assertNotIn('Свободны',text)
        self.assertIn('📍 Ваше место на линии «Горнозаводск»: 1',text)
        self.assertIn('1. 🟢 Водитель 1 (вы) — свободен — Горнозаводск',text)
        self.assertIn('2. 🟢 Водитель 2 — свободен — Горнозаводск',text)
        self.assertIn('\n\n1. 🟢 Водитель 3 — свободен — Пашия',text)

    def test_dispatcher_queue_has_same_rows_without_driver_rank_or_title(self):
        scope,text=self.fixture(dispatcher=True)
        self.assertNotIn('Очередь водителей',text);self.assertNotIn('Свободны',text)
        self.assertNotIn('Ваше место',text);self.assertNotIn('(вы)',text)
        self.assertTrue(text.startswith('1. 🟢 Водитель 1'))
        scope['kb'].dispatcher_menu.assert_called_once_with(False)

    def test_line_stays_visible_from_canonical_queue_not_wrong_display_flag(self):
        _,text=self.fixture()
        self.assertNotIn('НЕВЕРНАЯ ЛИНИЯ',text)
        self.assertIn('свободен — Горнозаводск — ⭐ 5.0 (7 отзывов)',text)
        self.assertIn('свободен — Пашия — ⭐ 5.0 (7 отзывов)',text)

    def test_empty_and_offline_only_lists_have_clear_no_driver_message(self):
        for mode in ('empty','offline'):
            with self.subTest(mode=mode):
                _,text=self.fixture(dispatcher=True,**{mode:True})
                self.assertEqual(text,'Сейчас нет водителей на линии.')

    def test_considering_notice_and_correct_main_driver_menu_remain(self):
        scope,text=self.fixture(considering=True)
        self.assertIn('Вы сейчас рассматриваете заявку',text)
        self.assertNotIn('Ваше место',text)
        scope['kb'].driver_menu.assert_called_once_with(True,False)
        self.assertNotIn('Свободны',text)

    def test_renderer_free_only_has_no_leading_or_trailing_blanks(self):
        scope=self.scope()
        d=NS(id=1,vk_id=101,full_name='Иван',current_line='Кусья')
        rows=scope['_render_driver_statuses']([dict(driver=d,status='online',position=1)],numbered=True,group_free_by_line=True)
        self.assertEqual(rows,['1. 🟢 Иван — свободен — Кусья — ⭐ 5.0 (7 отзывов)'])

    def test_nonfree_route_and_dispatcher_privacy_preserved(self):
        scope=self.scope();d=NS(id=1,vk_id=101,full_name='Иван',current_line='Горнозаводск')
        items=[dict(driver=d,status='busy',position=1,order=NS(),passenger_label='Клиент',order_id=1650)]
        driver='\n'.join(scope['_render_driver_statuses'](items,group_free_by_line=True))
        dispatcher='\n'.join(scope['_render_driver_statuses'](items,group_free_by_line=True,show_passenger_details=True))
        self.assertIn('до Чусового',driver);self.assertNotIn('Клиент',driver)
        self.assertIn('Клиент, заявка #1650',dispatcher)

    def override(self,template,function='show_queue'):
        text='1. 🟢 Иван — свободен — Пашия — ⭐ 5.0 (7 отзывов)'
        scope={'callsite_key':lambda *a:'key','DEFAULTS':{'key':('title','factory')},
               '_cached_rows':lambda:{'key':(template,None)},'_default_text':lambda k:'factory'}
        rendered=load('common/bot_messages_service.py','render_outgoing',scope)(text,'file',function,1,{})
        return text,rendered

    def test_saved_legacy_titles_cannot_restore_removed_headings(self):
        for template in ('📋 Очередь водителей:\n{default}','Свободны — Пашия:\n{default}',
                         'Свободны - Кусья:\n{default}','Свободны – Горнозаводск:\n{default}'):
            with self.subTest(template=template):
                default,rendered=self.override(template)
                self.assertEqual(rendered,default)

    def test_valid_custom_queue_template_still_works(self):
        default,rendered=self.override('{default}\nНа связи.')
        self.assertEqual(rendered,default+'\nНа связи.')

if __name__=='__main__':unittest.main()
