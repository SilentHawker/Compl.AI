"""
Run this script daily via Windows Task Scheduler or cron
"""
import sys
import os

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

from regulation_scraper import run_scraper_job

if __name__ == "__main__":
    print("Starting daily regulation scraper job...")
    results = run_scraper_job()
    print(f"Completed! Processed {len(results)} regulations.")