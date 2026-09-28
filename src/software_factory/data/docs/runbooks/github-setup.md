# Optional CI integration

Copy and review the example in .factory/templates/ci-python.yml.in into a new workflow only if GitHub CI is wanted. Initialization does not alter workflows or CODEOWNERS.

CI must hydrate the locked local runtime, validate exports and run the product checks in factory.json. The software-factory package's own tests validate the engine; they cannot substitute for product acceptance tests. Configure actual CODEOWNERS and branch rules through your repository's normal administration. Factory owner labels do not establish remote authorization.

No CI step automatically enables JEV or selects coding models. Remote CI results must refer to the tested commit; local READY_PR does not claim remote success.
