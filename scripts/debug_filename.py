
import os

def check_name(url):
    clean_url = url.split('?')[0]
    local_filename = clean_url.split('/')[-1]
    
    ext = os.path.splitext(local_filename)[1].lower()
    print(f"URL: {url}")
    print(f"Clean: {clean_url}")
    print(f"Base: {local_filename}")
    print(f"Ext: '{ext}' (Len: {len(ext)})")
    
    if not ext or len(ext) > 5:
        if not local_filename.lower().endswith('.pdf'):
            local_filename += ".pdf"
            print("  -> Appended .pdf")
    else:
        print("  -> Kept original extension")
    
    print(f"Final: {local_filename}\n")

# Test cases
check_name("https://example.com/ARCIL-2-17697597945858.jpg")
check_name("https://example.com/ARCIL-2-17697597945858.jpg?v=1")
check_name("https://example.com/doc.pdf")
check_name("https://example.com/unknown_file")
check_name("https://example.com/image.JPEG")
