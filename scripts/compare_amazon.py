"""Compare Amazon orders scraped from browser vs uncategorized CC transactions."""
import json
from datetime import datetime

# Amazon orders scraped from browser (107 orders, 2025)
amazon_orders = [
    {"date": "19 December 2025", "total": 500.0, "orderId": "408-5572706-4019550"},
    {"date": "19 December 2025", "total": 2000.0, "orderId": "408-2519653-6041954"},
    {"date": "16 December 2025", "total": 399, "orderId": "404-9070592-9985106"},
    {"date": "16 December 2025", "total": 899, "orderId": "404-5729949-6121946"},
    {"date": "16 December 2025", "total": 722.44, "orderId": "404-3922767-7199560"},
    {"date": "16 December 2025", "total": 763, "orderId": "404-1294147-3142744"},
    {"date": "5 December 2025", "total": 403, "orderId": "404-4194289-0392332"},
    {"date": "5 December 2025", "total": 780, "orderId": "404-0498694-5147552"},
    {"date": "23 November 2025", "total": 74999, "orderId": "408-3256402-1085127"},
    {"date": "23 November 2025", "total": 0, "orderId": "408-1469910-5087557"},
    {"date": "23 November 2025", "total": 444, "orderId": "408-1738236-3124328"},
    {"date": "23 November 2025", "total": 279, "orderId": "408-1712463-5210735"},
    {"date": "23 November 2025", "total": 958.4, "orderId": "408-1169664-2493940"},
    {"date": "23 November 2025", "total": 329, "orderId": "408-1022848-0649166"},
    {"date": "23 November 2025", "total": 420, "orderId": "408-0593212-5564311"},
    {"date": "14 October 2025", "total": 0.00, "orderId": "404-7049632-5097114"},
    {"date": "14 October 2025", "total": 6475, "orderId": "404-8425387-3521968"},
    {"date": "14 October 2025", "total": 659, "orderId": "404-6780517-1053928"},
    {"date": "14 October 2025", "total": 499, "orderId": "404-3547663-2100339"},
    {"date": "14 October 2025", "total": 1481.85, "orderId": "404-1528696-3481109"},
    {"date": "14 October 2025", "total": 356.25, "orderId": "404-1117223-6194706"},
    {"date": "14 October 2025", "total": 273, "orderId": "404-0663849-2505916"},
    {"date": "8 October 2025", "total": 273, "orderId": "408-7269642-0248364"},
    {"date": "8 October 2025", "total": 18999, "orderId": "408-0282683-1325115"},
    {"date": "7 October 2025", "total": 1623.55, "orderId": "408-8286303-8705940"},
    {"date": "5 October 2025", "total": 734.02, "orderId": "408-7367834-0872335"},
    {"date": "5 October 2025", "total": 3991.9, "orderId": "408-6884323-1698714"},
    {"date": "5 October 2025", "total": 3799, "orderId": "408-5220398-0581939"},
    {"date": "5 October 2025", "total": 6475, "orderId": "408-4952539-4895509"},
    {"date": "5 October 2025", "total": 836.36, "orderId": "408-1043517-3244338"},
    {"date": "1 October 2025", "total": 94, "orderId": "408-8111527-5054725"},
    {"date": "1 October 2025", "total": 3336, "orderId": "408-3099281-1617965"},
    {"date": "1 October 2025", "total": 2999, "orderId": "408-2264136-3140313"},
    {"date": "18 September 2025", "total": 229, "orderId": "404-8264316-6849130"},
    {"date": "18 September 2025", "total": 749.24, "orderId": "404-4601261-3148338"},
    {"date": "18 September 2025", "total": 245, "orderId": "404-0383464-5241902"},
    {"date": "17 September 2025", "total": 299.25, "orderId": "404-2492627-1251506"},
    {"date": "10 September 2025", "total": 728.16, "orderId": "404-6318496-0024338"},
    {"date": "10 September 2025", "total": 229, "orderId": "404-8826173-8164329"},
    {"date": "8 September 2025", "total": 0.00, "orderId": "408-8096290-8196356"},
    {"date": "8 September 2025", "total": 0.00, "orderId": "408-4885358-4481902"},
    {"date": "28 August 2025", "total": 226.1, "orderId": "404-2787004-7264361"},
    {"date": "26 August 2025", "total": 0, "orderId": "408-2127701-6602762"},
    {"date": "25 August 2025", "total": 226.1, "orderId": "404-1016662-8253921"},
    {"date": "23 August 2025", "total": 279.3, "orderId": "408-9296236-5033926"},
    {"date": "23 August 2025", "total": 28748, "orderId": "408-7550706-5599556"},
    {"date": "23 August 2025", "total": 437.71, "orderId": "408-7192785-4973969"},
    {"date": "23 August 2025", "total": 2474.01, "orderId": "408-7142663-0757941"},
    {"date": "23 August 2025", "total": 331.55, "orderId": "408-0971654-7219546"},
    {"date": "22 August 2025", "total": 0.00, "orderId": "406-3331449-9701912"},
    {"date": "19 August 2025", "total": 0.00, "orderId": "408-7175826-8393920"},
    {"date": "19 August 2025", "total": 0.00, "orderId": "408-4111554-7845960"},
    {"date": "28 July 2025", "total": 279.65, "orderId": "404-2263385-9613953"},
    {"date": "8 July 2025", "total": 449, "orderId": "408-5511111-8994757"},
    {"date": "8 July 2025", "total": 1379.04, "orderId": "408-4526386-5020305"},
    {"date": "8 July 2025", "total": 170.52, "orderId": "408-2088773-0907561"},
    {"date": "8 July 2025", "total": 1998, "orderId": "408-1023274-4201146"},
    {"date": "27 June 2025", "total": 541.5, "orderId": "405-5895488-3977960"},
    {"date": "27 June 2025", "total": 267.75, "orderId": "408-5022722-3761111"},
    {"date": "26 June 2025", "total": 319, "orderId": "408-9085880-4297166"},
    {"date": "26 June 2025", "total": 621, "orderId": "408-7618554-1326721"},
    {"date": "26 June 2025", "total": 225, "orderId": "408-7259262-9459548"},
    {"date": "26 June 2025", "total": 198, "orderId": "408-5469361-8046746"},
    {"date": "26 June 2025", "total": 788.5, "orderId": "408-4770466-4407501"},
    {"date": "26 June 2025", "total": 455.01, "orderId": "408-1746287-0702758"},
    {"date": "26 June 2025", "total": 149, "orderId": "408-1659780-0166741"},
    {"date": "27 May 2025", "total": 276.25, "orderId": "403-7117747-6301965"},
    {"date": "26 May 2025", "total": 450, "orderId": "408-5998726-5645136"},
    {"date": "8 May 2025", "total": 0.00, "orderId": "408-3420455-7274732"},
    {"date": "6 May 2025", "total": 673, "orderId": "408-6420372-7091530"},
    {"date": "6 May 2025", "total": 915, "orderId": "408-5685130-0354729"},
    {"date": "6 May 2025", "total": 245, "orderId": "408-4877588-5341950"},
    {"date": "6 May 2025", "total": 228, "orderId": "408-3771349-2925961"},
    {"date": "6 May 2025", "total": 1282, "orderId": "408-1542651-4325101"},
    {"date": "26 April 2025", "total": 331.5, "orderId": "408-7244017-5617908"},
    {"date": "11 April 2025", "total": 541.5, "orderId": "404-5621570-3170710"},
    {"date": "28 March 2025", "total": 331.5, "orderId": "408-9205749-5754747"},
    {"date": "28 March 2025", "total": 237.6, "orderId": "406-2615532-7220342"},
    {"date": "28 March 2025", "total": 376.2, "orderId": "406-9551829-9597969"},
    {"date": "28 March 2025", "total": 237.6, "orderId": "404-5138232-0796369"},
    {"date": "28 March 2025", "total": 252, "orderId": "407-7450054-9937902"},
    {"date": "7 March 2025", "total": 284, "orderId": "404-9963639-6235523"},
    {"date": "7 March 2025", "total": 2715, "orderId": "404-7292332-9808357"},
    {"date": "7 March 2025", "total": 1299, "orderId": "404-6352439-4405927"},
    {"date": "7 March 2025", "total": 712.8, "orderId": "404-4633250-0058768"},
    {"date": "7 March 2025", "total": 337, "orderId": "404-3133776-6379511"},
    {"date": "7 March 2025", "total": 652.91, "orderId": "404-2379366-1910715"},
    {"date": "7 March 2025", "total": 470, "orderId": "404-1776172-2413154"},
    {"date": "26 February 2025", "total": 2199, "orderId": "407-8972038-7174702"},
    {"date": "26 February 2025", "total": 11995, "orderId": "407-7139145-2929962"},
    {"date": "26 February 2025", "total": 9495, "orderId": "407-7011587-8062709"},
    {"date": "25 February 2025", "total": 418, "orderId": "171-0123468-5636323"},
    {"date": "25 February 2025", "total": 326.4, "orderId": "403-8358501-2523555"},
    {"date": "14 February 2025", "total": 232.75, "orderId": "408-1530658-8351544"},
    {"date": "9 February 2025", "total": 16999, "orderId": "407-0595628-7691535"},
    {"date": "6 February 2025", "total": 325, "orderId": "404-9236579-5561106"},
    {"date": "6 February 2025", "total": 925.44, "orderId": "404-5255316-2312301"},
    {"date": "6 February 2025", "total": 1350, "orderId": "404-0262409-9328303"},
    {"date": "30 January 2025", "total": 94999, "orderId": "405-5365177-2721117"},
    {"date": "30 January 2025", "total": 221.85, "orderId": "404-0251461-9525179"},
    {"date": "30 January 2025", "total": 0.00, "orderId": "404-9373747-1398705"},
    {"date": "28 January 2025", "total": 246.6, "orderId": "407-4605580-0464317"},
    {"date": "28 January 2025", "total": 245.7, "orderId": "408-8038008-7757959"},
    {"date": "28 January 2025", "total": 246.6, "orderId": "405-9254365-3449936"},
    {"date": "7 January 2025", "total": 2198, "orderId": "407-7657409-0430727"},
    {"date": "7 January 2025", "total": 390, "orderId": "407-5525061-6467548"},
    {"date": "3 January 2025", "total": 2889, "orderId": "171-2741701-6270729"},
]

# Parse dates
for o in amazon_orders:
    o["parsed_date"] = datetime.strptime(o["date"], "%d %B %Y")

# Uncategorized Amazon CC transactions
cc_txns = [
    {"card": "Kotak", "date": "2025-11-06", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Kotak", "date": "2025-10-14", "amount": 9744.1, "desc": "AMAZON PAY INDIA PRIVA Bangalore"},
    {"card": "Kotak", "date": "2025-10-06", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Kotak", "date": "2025-09-19", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Kotak", "date": "2025-08-06", "amount": 199.0, "desc": "AMAZON PRIME MUMBAI"},
    {"card": "Kotak", "date": "2025-07-10", "amount": 399.0, "desc": "AMAZON INDIA CYBS SI"},
    {"card": "Kotak", "date": "2025-07-07", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Kotak", "date": "2025-06-27", "amount": 541.5, "desc": "AMAZON INDIA CYBS SI"},
    {"card": "Kotak", "date": "2025-06-07", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Kotak", "date": "2025-05-08", "amount": 199.0, "desc": "AMAZON INDIA CYBS SI (Prime sub)"},
    {"card": "Mayura", "date": "2025-08-10", "amount": 19272.0, "desc": "AMAZON PAY INDIA PRIVA Bangalore"},
    {"card": "Mayura", "date": "2025-06-26", "amount": 2755.51, "desc": "AMAZON PAY INDIA PRIVA Bangalore"},
    {"card": "Mayura", "date": "2025-06-26", "amount": 3679.72, "desc": "AMAZON MKTPLACE PMTS Amzn.com/bill"},
    {"card": "Mayura", "date": "2025-06-19", "amount": 6978.06, "desc": "AMAZON MARK* NO9TV13F1 SEATTLE"},
    {"card": "Mayura", "date": "2025-06-17", "amount": 31302.4, "desc": "AMAZON MARK* NO15L6PA2 SEATTLE"},
    {"card": "Mayura", "date": "2025-06-16", "amount": 3688.62, "desc": "AMAZON MKTPL*NA0ZG4MI0 Amzn.com/bill"},
]

print("=" * 100)
print("AMAZON CC TRANSACTIONS vs AMAZON ORDERS - MATCHING ANALYSIS")
print("=" * 100)

matched = []
unmatched = []
used_orders = set()

for txn in cc_txns:
    txn_date = datetime.strptime(txn["date"], "%Y-%m-%d")
    txn_amt = txn["amount"]

    # Find exact amount match within +/- 10 days
    best_match = None
    for i, o in enumerate(amazon_orders):
        if i in used_orders:
            continue
        if abs(o["total"] - txn_amt) < 1.0:  # exact match (within 1 rupee)
            day_diff = abs((o["parsed_date"] - txn_date).days)
            if day_diff <= 15:
                if best_match is None or day_diff < best_match[2]:
                    best_match = (o, i, day_diff)

    if best_match:
        matched.append((txn, best_match[0], best_match[2]))
        used_orders.add(best_match[1])
    else:
        # Try close amount match (within 10%)
        close_matches = []
        for i, o in enumerate(amazon_orders):
            if i in used_orders or o["total"] == 0:
                continue
            if txn_amt > 0 and abs(o["total"] - txn_amt) / txn_amt < 0.10:
                day_diff = abs((o["parsed_date"] - txn_date).days)
                if day_diff <= 20:
                    close_matches.append((o, day_diff, abs(o["total"] - txn_amt)))
        close_matches.sort(key=lambda x: (x[2], x[1]))
        unmatched.append((txn, close_matches[:3]))

print()
print(f"EXACT MATCHES: {len(matched)} / {len(cc_txns)}")
print("-" * 100)
for txn, order, days in matched:
    print(f"  {txn['card']:8s} | CC: {txn['date']} Rs{txn['amount']:>10,.2f} | Order: {order['date']} Rs{order['total']:>10,.2f} | {order['orderId']} | {days}d gap")

print()
print(f"NO EXACT MATCH: {len(unmatched)} / {len(cc_txns)}")
print("-" * 100)
for txn, close in unmatched:
    print(f"  {txn['card']:8s} | CC: {txn['date']} Rs{txn['amount']:>10,.2f} | {txn['desc']}")
    if close:
        for o, days, diff in close:
            print(f"           Close: {o['date']} Rs{o['total']:>10,.2f} | diff Rs{diff:.2f} | {days}d gap | {o['orderId']}")
    else:
        print(f"           No close match found in Amazon orders")

print()
print("=" * 100)
print("SUMMARY BY TYPE:")
print("-" * 60)
prime_txns = [t for t in cc_txns if "CYBS" in t["desc"] or "PRIME" in t["desc"]]
pay_txns = [t for t in cc_txns if "PAY" in t["desc"]]
mkt_txns = [t for t in cc_txns if "MKT" in t["desc"] or "MARK" in t["desc"]]
print(f"  Amazon Prime/CYBS (subscriptions): {len(prime_txns)} txns, Rs{sum(t['amount'] for t in prime_txns):,.2f}")
print(f"  Amazon Pay (purchases):            {len(pay_txns)} txns, Rs{sum(t['amount'] for t in pay_txns):,.2f}")
print(f"  Amazon Marketplace (USD charges):  {len(mkt_txns)} txns, Rs{sum(t['amount'] for t in mkt_txns):,.2f}")
print(f"  TOTAL:                             {len(cc_txns)} txns, Rs{sum(t['amount'] for t in cc_txns):,.2f}")
print()
print(f"  Matched:   {len(matched)} txns")
print(f"  Unmatched: {len(unmatched)} txns (Rs{sum(t[0]['amount'] for t in unmatched):,.2f})")
