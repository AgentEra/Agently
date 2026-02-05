"""Agently + claw2claw receipts demo.

Goal: produce a verifiable proof link (preview + download) for an agent output.

Env:
  export C2C_API_KEY=...
  export C2C_BOT_ID=bot_...
"""
import os
from claw2claw_receipt import create_offer, create_job, post_receipt


def main():
    bot_id = os.environ["C2C_BOT_ID"]

    offer = create_offer(
        seller_bot_id=bot_id,
        title="Agently: proof-of-delivery demo",
        description="Demo: mint a verifiable receipt for an agent output.",
        price_cents=0,
        tags=["demo"],
        capabilities=["receipt_demo"],
    )

    job = create_job(
        offer_id=offer["offerId"],
        buyer_bot_id=bot_id,
        idempotency_key="agently_demo_v1",
    )

    proof = post_receipt(
        job_id=job["jobId"],
        status="ok",
        artifacts={"result.md": "# Agently\n\nThis output now has a verifiable receipt link.\n"},
    )

    print("Proof link:", proof["proofUrl"])


if __name__ == "__main__":
    main()
