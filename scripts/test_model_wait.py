"""Model waits must terminate even while a stream keeps sending heartbeat bytes."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.api.error_protocol import DomainApiError
from backend.services.novel import model_wait as wait


class ModelWaitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = {}
        self.events = []
        self.cancelled = asyncio.Event()
        self.entered = asyncio.Event()
        self.patches = [patch.object(wait, "POLL_SECONDS", .005),
                        patch.object(wait, "PROGRESS_SECONDS", .01),
                        patch.object(wait, "STOP_GRACE_SECONDS", .04)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def control(self):
        return self.state

    async def progress(self, event):
        self.events.append(event)

    async def hanging(self):
        self.entered.set()
        try:
            # A transport read timeout would never fire on this heartbeat stream.
            while True:
                await asyncio.sleep(.001)
        finally:
            self.cancelled.set()

    async def run_call(self, call=None, **kwargs):
        return await wait.wait_for_model(call or self.hanging, control=self.control,
            progress=self.progress, label="提取章后事实", **kwargs)

    async def test_total_deadline_cancels_heartbeat_stream_and_logs_timeout(self):
        with self.assertRaises(DomainApiError) as failure:
            await asyncio.wait_for(self.run_call(total_seconds=.05), 1)
        self.assertEqual(failure.exception.code, "VOLUME_WRITING_MODEL_TIMEOUT")
        self.assertEqual(failure.exception.status_code, 504)
        self.assertTrue(self.cancelled.is_set())
        self.assertTrue(any(event["state"] == "running" for event in self.events))
        self.assertEqual(self.events[-1]["state"], "failed")

    async def test_pause_and_cancel_stop_an_unfinished_call_after_grace(self):
        for flag in ("pause_requested", "cancel_requested"):
            with self.subTest(flag=flag):
                self.state = {}
                self.entered.clear()
                self.cancelled.clear()
                task = asyncio.create_task(self.run_call(total_seconds=10))
                await self.entered.wait()
                self.state[flag] = True
                with self.assertRaises(wait.ModelWaitStopped):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(self.cancelled.is_set())
                self.assertEqual(self.events[-1]["state"], "stopped")

    async def test_result_returning_during_pause_is_kept(self):
        gate = asyncio.Event()
        async def complete():
            self.entered.set()
            await gate.wait()
            return {"extraction": "paid output"}
        task = asyncio.create_task(self.run_call(complete, total_seconds=10))
        await self.entered.wait()
        self.state["pause_requested"] = True
        gate.set()
        self.assertEqual(await task, {"extraction": "paid output"})

    async def test_preexisting_pause_never_sends_a_model_request(self):
        self.state["pause_requested"] = True
        call = AsyncMock()
        with self.assertRaises(wait.ModelWaitStopped):
            await self.run_call(call)
        call.assert_not_called()

    async def test_worker_cancellation_closes_the_child_request(self):
        task = asyncio.create_task(self.run_call(total_seconds=10))
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.cancelled.is_set())

    async def test_broken_stream_cleanup_cannot_block_pause_forever(self):
        release = asyncio.Event()
        async def stubborn_cleanup():
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                self.cancelled.set()
        with patch.object(wait, "CLEANUP_SECONDS", .02):
            task = asyncio.create_task(self.run_call(stubborn_cleanup, total_seconds=10))
            await self.entered.wait()
            self.state["pause_requested"] = True
            try:
                with self.assertRaises(wait.ModelWaitStopped):
                    await asyncio.wait_for(task, 1)
            finally:
                release.set()
                await asyncio.wait_for(self.cancelled.wait(), 1)

    async def test_losing_ownership_cancels_child_without_success(self):
        async def control():
            if self.entered.is_set():
                raise asyncio.CancelledError()
            return {}
        with self.assertRaises(asyncio.CancelledError):
            await wait.wait_for_model(self.hanging, control=control, progress=self.progress,
                                      label="test", total_seconds=10)
        self.assertTrue(self.cancelled.is_set())

    async def test_success_and_model_failure_propagate_unchanged(self):
        self.assertEqual(await self.run_call(AsyncMock(return_value=42)), 42)
        error = ValueError("model output invalid")
        with self.assertRaises(ValueError) as failure:
            await self.run_call(AsyncMock(side_effect=error))
        self.assertIs(failure.exception, error)


if __name__ == "__main__":
    unittest.main()
