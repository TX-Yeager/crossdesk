"""Bound pending remote positions without crossing input or ownership barriers."""
import queue


class RoutingQueue(queue.Queue):
    def __init__(self,maxsize=4096):
        super().__init__(maxsize)
        self.pointer_coalesced=0

    @staticmethod
    def _position_key(item):
        kind,message=item
        if kind!='network' or message.get('type')!='pointer' or message.get('coalesce') is not True:
            return None
        return message.get('screen'),message.get('focus_revision')

    def _put(self,item):
        key=self._position_key(item)
        if key is not None and self.queue and self._position_key(self.queue[-1])==key:
            self.queue[-1]=item
            # Queue.put increments unfinished_tasks after _put. Replacing a
            # pending position does not create another consumer task.
            self.unfinished_tasks-=1
            self.pointer_coalesced+=1
        else:
            super()._put(item)
