
import os

DOWNLOAD_DIR = "e:/01_vibe_coding/08_auction/downloads"

def fix_extensions():
    count = 0
    if not os.path.exists(DOWNLOAD_DIR):
        print(f"Directory {DOWNLOAD_DIR} not found.")
        return

    for fname in os.listdir(DOWNLOAD_DIR):
        if fname.lower().endswith(".jpg.pdf"):
            old_path = os.path.join(DOWNLOAD_DIR, fname)
            new_name = fname[:-4] # Remove .pdf
            new_path = os.path.join(DOWNLOAD_DIR, new_name)
            
            try:
                os.rename(old_path, new_path)
                print(f"Renamed: {fname} -> {new_name}")
                count += 1
            except Exception as e:
                print(f"Error renaming {fname}: {e}")
    
    print(f"\nTotal files renamed: {count}")

if __name__ == "__main__":
    fix_extensions()
