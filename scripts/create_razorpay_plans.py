"""Create the monthly Basic and Pro plans in Razorpay (run once per account / mode).

    RAZORPAY_KEY_ID=… RAZORPAY_KEY_SECRET=… python -m scripts.create_razorpay_plans

Prices come from PLAN_BASIC_PRICE_INR / PLAN_PRO_PRICE_INR (default ₹99 / ₹299).
Prints the two env lines to paste into Render. Test-mode keys create test plans.
"""

import asyncio

from app import config
from services import billing


async def main() -> None:
    if not (config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET):
        raise SystemExit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET first.")
    lines = []
    for plan in billing.plans().values():
        created = await billing._razorpay("POST", "/plans", json={
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": f"Reel Brain {plan.title}",
                "amount": plan.price_inr * 100,  # paise
                "currency": "INR",
                "description": f"{plan.reels} reels a month",
            },
            "notes": {"plan": plan.key},
        })
        lines.append(f"RAZORPAY_PLAN_{plan.key.upper()}={created['id']}")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
