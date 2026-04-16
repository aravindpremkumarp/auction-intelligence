
import os
import sys
import time

PAUSE_FILE = "scraper_pause.lock"
STOP_FILE = "scraper_stop.lock"

def show_help():
    print("\n--- Scraper Controls ---")
    print("1. [P]ause Scraper")
    print("2. [R]esume Scraper")
    print("3. [S]top Scraper")
    print("4. [C]heck Status")
    print("5. [E]xit Control")

def manage():
    while True:
        show_help()
        choice = input("\nEnter choice: ").strip().lower()
        
        if choice in ['1', 'p']:
            with open(PAUSE_FILE, 'w') as f: f.write("PAUSED")
            print(">> PAUSE Signal Sent. Scraper will pause after current batch.")
            
        elif choice in ['2', 'r']:
            if os.path.exists(PAUSE_FILE):
                os.remove(PAUSE_FILE)
                print(">> RESUME Signal Sent.")
            else:
                print(">> Scraper is not paused.")
                
        elif choice in ['3', 's']:
            confirm = input("Are you sure you want to STOP the scraper? (y/n): ")
            if confirm.lower() == 'y':
                with open(STOP_FILE, 'w') as f: f.write("STOP")
                print(">> STOP Signal Sent. Scraper will exit soon.")
        
        elif choice in ['4', 'c']:
            if os.path.exists(PAUSE_FILE):
                print("STATUS: PAUSED ⏸️")
            elif os.path.exists(STOP_FILE):
                print("STATUS: STOPPING 🛑")
            else:
                print("STATUS: RUNNING 🟢 (Assuming script is active)")
                
        elif choice in ['5', 'e']:
            break
        else:
            print("Invalid choice.")
            
if __name__ == "__main__":
    manage()
