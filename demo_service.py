"""Demo 'payments settlement' service. It crashes at night, when there are no transactions."""
import os
import time

def average_ticket(total, count):
    # BUG: No check for count == 0. Throws ZeroDivisionError.
    return total / count

def main():
    print("settlement service started", flush=True)
    for tick in range(1, int(os.getenv("DEMO_TICKS", "1000000"))):
        time.sleep(1)
        transactions = [] # night time: no transactions arrive
        avg = average_ticket(sum(transactions), len(transactions))
        print(f"tick {tick}: average ticket = {avg}", flush=True)

if __name__ == "__main__":
    main()
