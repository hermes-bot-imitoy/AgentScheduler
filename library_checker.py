import pkg_resources
import sys

def check_libraries():
    """Checks for the installation of common libraries and prints version info."""
    libraries_to_check = [
        "numpy",
        "pandas",
        "matplotlib",
        "requests"
    ]
    print("--- Library Version Checker ---")
    print(f"Running on Python version: {sys.version.split()[0]}")
    print("-" * 35)

    found_count = 0
    for lib in libraries_to_check:
        try:
            version = pkg_resources.get_distribution(lib).version
            print(f"✅ {lib}: Installed (Version {version})")
            found_count += 1
        except pkg_resources.DistributionNotFound:
            print(f"❌ {lib}: Not found. Please install with 'pip install {lib}'.")

    print("-" * 35)
    if found_count > 0:
         print("\nScript executed successfully. Check the list above for details.")
    else:
         print("\nCould not find any checked libraries. Please ensure your environment is set up.")

if __name__ == "__main__":
    check_libraries()