import app
from azure.core.exceptions import ResourceNotFoundError


def test_polling_a_queue_that_was_never_created_creates_it_instead_of_failing():
    calls = []

    class Queue:
        def receive_messages(self, **kwargs):
            raise ResourceNotFoundError('The specified queue does not exist.')

        def create_queue(self):
            calls.append('create')

    assert app._poll_clustering_queue_once(Queue(), 'photostore-clustering-deferred', 5, None) is False
    assert calls == ['create']
