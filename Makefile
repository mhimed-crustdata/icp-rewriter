.PHONY: setup run demo test
setup:
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY, then export your accounts to accounts.csv"
run:
	python3 icp.py --notion
demo:
	python3 icp.py --dry-run --all
test:
	python3 -m unittest discover -s tests -v
