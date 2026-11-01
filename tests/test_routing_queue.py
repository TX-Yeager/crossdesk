import unittest
from crossdesk.routing_queue import RoutingQueue


class RoutingQueueTests(unittest.TestCase):
    def pointer(self,value,screen='mac',revision=None,drag=False):
        return ('network',{'type':'pointer','screen':screen,'focus_revision':revision or [1,'win'],
                           'coalesce':not drag,'u':value,'v':.5})

    def test_high_rate_remote_positions_do_not_build_seconds_of_stale_motion(self):
        queue=RoutingQueue()
        focus=('network',{'type':'focus'})
        click=('network',{'type':'input','event':{'type':'button','down':True}})
        queue.put_nowait(focus)
        for n in range(8000):queue.put_nowait(self.pointer(n/8000))
        queue.put_nowait(click)
        for n in range(8000):queue.put_nowait(self.pointer(1-n/8000))
        self.assertEqual(queue.qsize(),4)
        self.assertEqual(queue.pointer_coalesced,15998)
        result=[]
        while not queue.empty():result.append(queue.get_nowait());queue.task_done()
        self.assertEqual(result,[focus,self.pointer(7999/8000),click,self.pointer(1-7999/8000)])
        self.assertEqual(queue.unfinished_tasks,0)

    def test_focus_screen_direction_and_drag_barriers_are_preserved(self):
        queue=RoutingQueue()
        events=[self.pointer(.1),self.pointer(.2,screen='other'),self.pointer(.3,revision=[2,'win']),
                ('local',{'type':'move','dx':-1}),self.pointer(.4,drag=True),self.pointer(.5,drag=True),
                ('network',{'type':'release'}),self.pointer(.6)]
        for item in events:queue.put_nowait(item)
        self.assertEqual([queue.get_nowait() for _ in events],events)
