"""The project the CI job's dry run of the GitHub Action reads (.github/workflows/ci.yml, job
action). Its store is in store/: accept_terms has a heal a passing test confirmed, email a
heal nothing confirmed, and pay is marked destructive. So the dry run would write back one
selector and leave two keys alone. The file is not named test_*.py, so pytest does not
collect it."""


def sign_up(heal):
    heal.locator("email", "#email").fill("ada@example.com")
    heal.locator("accept_terms", "#accept-terms", role="checkbox").check()
    heal.locator("pay", "#pay", role="button").click()
