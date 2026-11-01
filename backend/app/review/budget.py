"""One request gate for discovery, verification, corrections and overview."""

import asyncio
from dataclasses import dataclass

from app.review.domain import utcnow
from app.review.store import BudgetExceeded
from app.review.observability import event, settlement


@dataclass
class BudgetGate:
    store: object
    run: object
    lease: dict

    async def active(self):
        await asyncio.to_thread(self.store.assert_active, self.run.id, self.lease)
        if utcnow() >= self.run.hard_deadline:
            raise BudgetExceeded("hard_deadline")

    async def reserve(self, work, estimate, *, overview=False, with_remaining=False):
        try:
            result = await asyncio.to_thread(self.store.reserve, self.run.id, work,
                                            estimate, self.lease, overview=overview,
                                            with_remaining=with_remaining)
        except BudgetExceeded as exc:
            event("budget.rejected", reason=str(exc), estimated_input_tokens=estimate)
            raise
        event("budget.reserved", ticket=result[0] if with_remaining else result,
              estimated_input_tokens=estimate)
        return result

    async def headroom(self, work):
        return await asyncio.to_thread(self.store.request_headroom, self.run.id, work, self.lease)

    async def settle(self, ticket, usage):
        receipt = await asyncio.to_thread(self.store.settle, self.run.id, ticket, usage, self.lease)
        settlement(ticket, receipt)
