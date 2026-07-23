!! - At this time the bot is experimental and does not give reliable returns, examine and test with caution. You are responsible for your losses.

Jeremiah here.

Program classification: MFT (Medium Frequency Trading) default 1 minute intervals.

Requires a Charles Schwab brokerage account with developer API access and margin access. It utilizes shorting which requires a margin account - this will probably be changed for greater accessibility.

Strategy: "SMA Direction Toggler"
This trading program was made to try to use calculus, namely derivatives, of price functions of equities (stocks) to try to buy low sell high. To smooth datapoints, it uses simple moving averages. Utilizes a toggle mechanism where it will try three different moving averages. it will compare whether a moving average changes from increasing to decreasing and buy and sell accordingly.

This program utilizes the schwabdev library by Tyler Bowers, who has created documentation, YouTube tutorials, and more. Check it out: https://tylerebowers.github.io/Schwabdev/

At this time the program is missing a blank .env for API key connection, this will be added soon.

