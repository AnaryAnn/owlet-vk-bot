import json
import unittest
from unittest.mock import Mock, patch
import quiet_vote_test as quiet


class QuietVoteTests(unittest.TestCase):
    def test_callback_keyboard_targets_only_test_chat(self):
        send = Mock()
        quiet.publish(send, 'token', 30)
        self.assertEqual(send.call_args.args[0], 2000000001)
        buttons = send.call_args.kwargs['keyboard']['buttons']
        self.assertTrue(all(b[0]['action']['type'] == 'callback' for b in buttons))
        payload = json.loads(buttons[0][0]['action']['payload'])
        event = dict(peer_id=quiet.PEER_ID, user_id=123, event_id='event', payload=payload)
        feedback = Mock()
        self.assertTrue(quiet.handle(event, 'token', feedback))
        feedback.assert_called_once_with(123, quiet.PEER_ID, 'event', '🗳 Тестовый голос принят: вариант 1.')

    def test_wrong_chat_or_forged_button_does_not_send_anything(self):
        expires = 1000
        good = dict(kind='quiet_vote_test', expires=expires, choice=1,
                    signature=quiet.signature('token', expires))
        feedback = Mock()
        with patch.object(quiet.time, 'time', return_value=900):
            for peer, token in [(2000000002, 'token'), (quiet.PEER_ID, 'wrong')]:
                quiet.handle(dict(peer_id=peer, user_id=123, event_id='event', payload=good), token, feedback)
        feedback.assert_not_called()
