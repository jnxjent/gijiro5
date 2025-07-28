# utils/timer.py
import time

class Timer:
    def __init__(self):
        self.marks = []

    def mark(self, label: str):
        now = time.time()
        self.marks.append((label, now))
        print(f"[TIMER] {label}: {now:.3f}")

    def report(self):
        print("[TIMER REPORT]")
        for i in range(1, len(self.marks)):
            label_prev, t_prev = self.marks[i - 1]
            label_curr, t_curr = self.marks[i]
            delta = t_curr - t_prev
            print(f"  {label_prev} → {label_curr}: {delta:.3f} 秒")
