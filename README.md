Jeremiah here.

This trading algorithm was made to try to use calculus, namely derivatives, of the price functions of equities to try to buy low sell high. To smooth datapoints, the program uses simple moving averages. At this time the bot is highly experimental and does not give reliable returns, test at your own risk.

this program utilizes the schwabdev library by Tyler Bowers. he has created documentation, YouTube tutorials, and more. Check it out:

at this time the program is missing a blank .env for API key connection this will be added soon.

the program utilizes a toggle mechanism where it will try three different moving averages. it will compare whether a moving average changes from increasing to decreasing and buy and sell accordingly.

this program does utilize shorting which requires a margin account. this will probably be changed for greater accessibility 