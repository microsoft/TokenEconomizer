"""Maven and Gradle runs the loop guard counts as verifications, and builds told to skip the tests.

`./gradlew :app:test` runs a subproject's test task, so a failing one after the last edit must
leave the turn not green. `./gradlew build -x test` leaves the test task out and `mvn test
-DskipTests` skips the tests: both pass without running any, so neither may stand in for a test
run, and a passing one must not clear a test run that failed before it."""
import pytest

from tokeneconomizer import loopguard as guard
from tokeneconomizer.loopguard import turn

RUNS = [
    "mvn test", "mvn -q -B clean test", "mvn -q test -Dtest=FooTest", "./mvnw -P ci test",
    "mvn test -DskipTests=false", "mvn test -D skipTests=false",
    "mvn test -DSKIPTESTS",                           # Maven reads the name as written
    "./gradlew test", "./gradlew --offline -i clean test", "./gradlew -x javadoc test",
    "./gradlew test -DskipTests",                     # a Maven property; Gradle runs the tests
    "./gradlew :test", "./gradlew :app:test", "./gradlew app:test",
    "gradle build -x javadoc :app:test",
    "./gradlew clean :lib:util:test --tests com.acme.FooTest",
]

NOT_RUNS = [
    "./gradlew build -x test", "gradle -x test build", "./gradlew assemble --exclude-task test",
    "./gradlew build --exclude-task=test", "./gradlew :app:build -x :app:test",
    "./gradlew :app:build -x:app:test", "./gradlew :app:build --exclude-task=:app:test",
    "./gradlew :test:compileJava",                    # a subproject named `test`
    "./gradlew :test-utils:compileJava", "./gradlew testClasses",
    "mvn test -DskipTests", "mvn -DskipTests test", "./mvnw -q test -Dmaven.test.skip=true",
    "mvn test -Dmaven.test.skip.exec=true", "mvn clean install -DskipTests",
    "mvn test -D skipTests", "mvn test --define skipTests", "mvn test --define=skipTests",
    "mvn test -DskipTests=TRUE", "mvn test -Dmaven.test.skip=True",
]


def _edited():
    g = guard.TurnGuard(turn_key="t")
    g.fold("Edit", {"file_path": "/r/src/A.java", "old_string": "x", "new_string": "y"}, "ok")
    return g


def _out(cmd, ok):
    if "mvn" in cmd:
        return "BUILD SUCCESS" if ok else "BUILD FAILURE"
    return "BUILD SUCCESSFUL" if ok else "BUILD FAILED"


@pytest.mark.parametrize("cmd", RUNS)
def test_a_maven_or_gradle_test_run_counts_as_a_verification(cmd):
    assert turn.status_of_tests(cmd) == "own"
    g = _edited()
    g.fold("Bash", {"command": cmd}, _out(cmd, False), 1)
    assert g.verifications == 1 and not g.resolved_proxy()


def test_a_subproject_test_run_whose_status_a_pipe_replaced_is_read_by_its_output():
    cmd = "./gradlew :app:test 2>&1 | tail -20"
    assert turn.status_of_tests(cmd) == "hidden"
    g = _edited()
    g.fold("Bash", {"command": cmd}, "> Task :app:test FAILED\n\n1 test completed, 1 failed\n\n"
                                     "FAILURE: Build failed with an exception.\n\n"
                                     "BUILD FAILED in 3s", 0)
    assert g.verifications == 1 and not g.resolved_proxy()


@pytest.mark.parametrize("cmd", NOT_RUNS)
def test_a_build_that_runs_no_tests_is_not_a_verification(cmd):
    assert turn.status_of_tests(cmd) is None


@pytest.mark.parametrize("failed, skipped", [("./gradlew test", "./gradlew build -x test"),
                                             ("mvn test", "mvn test -DskipTests")])
def test_a_build_that_skips_the_tests_does_not_clear_a_failing_run(failed, skipped):
    g = _edited()
    g.fold("Bash", {"command": failed}, _out(failed, False), 1)
    g.fold("Bash", {"command": skipped}, _out(skipped, True), 0)
    assert g.verifications == 1 and not g.resolved_proxy()


def test_a_skip_in_a_later_command_does_not_hide_an_earlier_test_run():
    assert guard.VERIFY_RE.search("mvn test && mvn install -DskipTests")
    assert turn.status_of_tests("mvn test && mvn install -DskipTests") == "own"
