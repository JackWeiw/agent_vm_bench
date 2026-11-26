# Coding Benchmark Template (Java) - E2B Sandbox for AI Coding Agent Memory Stress Testing
#
# Environment: Pure Java coding benchmark (NO browser/Chromium, NO agent tools,
# NO resident dev server, NO production packaging step)
#
# Project: google/gson (https://github.com/google/gson)
#   - Real swe_bench_multilingual instance google__gson-1014
#     (JsonTreeReader.skipValue -> ArrayIndexOutOfBoundsException on an empty
#     root object), cloned at base_commit 7719e73a.
#   - Toolchain: JDK + Maven (the toolchain an agent touches).
#
# Trace-faithful loop (mirrors the ts/go/python variants): find -> read -> edit
# -> verify -> git diff. Verify runs the instance's REAL Maven tests
# (FAIL_TO_PASS; PASS_TO_PASS when the instance declares any) through
# `mvn -o test` -- the project's own JUnit harness, not a hand-written probe.
# That transient surefire/JVM compile+run peak is what N sandboxes overlap. No
# `mvn package`, no full module suite, no resident process.
#
# JDK choice: the 2017 poms declare <java.version>1.6</java.version>, which
# javac 8+ rejects. We install JDK 11 and override the property to 8 at every
# compile (-Djava.version=8), which both build-time prewarm and the runtime
# verify command do.
#
# Build:  docker build -t ubuntu-openclaw-coding-java:24.04-linuxarm64 -f Dockerfile .
# Push:   HARBOR_IP=X bash push_to_harbor.sh
# Template: python3 ../../build_e2b.py --server-ip X --harbor-ip X \
#     --alias openclaw-coding-java-v1 --image e2b-orchestration/ubuntu-coding-java-bench:custom

FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/ubuntu:24.04-linuxarm64

# Set environment variables to avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Configure proxy (empty by default, set via --build-arg if needed)
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ENV http_proxy=${HTTP_PROXY}
ENV https_proxy=${HTTPS_PROXY}
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

# Install JDK 11 + Maven + git (see Dockerfile.x86 for the full rationale:
# corp proxy MITMs TLS -> git sslVerify=false, Maven aliyun mirror + insecure
# wagon flags; PATH/JAVA_HOME pin the JDK so apt's default-jre is not used).
# python3 is REQUIRED at runtime (not just build time): the coding runner's edit
# step rewrites the target file with a `python3 - <<'PYEOF'` literal replace, and
# the verify step parses surefire XML with python3. ubuntu:24.04 ships none.
#
# Toolchain versions are PINNED (see Dockerfile.x86 for the full rationale: JDK
# is the JVM baseline, Maven 3.8.7's super POM binds surefire 2.12.4, so an
# unpinned install would drift on a rebuild elsewhere). The values are the same
# deb versions as the amd64 build; if the arm64 archive differs, apt names the
# requested version in its error -- adjust the ARG, do not drop the pin.
ARG JDK_VER=11.0.33+9-1ubuntu1~24.04.1
ARG MAVEN_VER=3.8.7-2
ARG PYTHON_VER=3.12.3-0ubuntu2.1

RUN apt update && \
    apt install -y --no-install-recommends \
        openjdk-11-jdk=${JDK_VER} maven=${MAVEN_VER} python3=${PYTHON_VER} \
        git curl vim ca-certificates && \
    git config --global http.sslVerify false && \
    apt clean && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-arm64
ENV PATH=${JAVA_HOME}/bin:${PATH}

# Fail the build (instead of silently drifting) if PATH/JAVA_HOME resolve a
# different toolchain than the pinned one -- mirrors the ts image's
# `pnpm --version | grep -qx` check.
RUN java -version 2>&1 | grep -q '11\.0\.33' || (echo "ERROR: JDK is not 11.0.33: $(java -version 2>&1 | head -1)" && exit 1)
RUN mvn -v | head -1 | grep -q 'Apache Maven 3\.8\.7' || (echo "ERROR: Maven is not 3.8.7: $(mvn -v | head -1)" && exit 1)
RUN echo "Toolchain check: JDK 11.0.33 + Maven 3.8.7 (pinned)"

COPY settings.xml /root/.m2/settings.xml
ENV MAVEN_OPTS="-Dmaven.wagon.http.ssl.insecure=true -Dmaven.wagon.http.ssl.allowall=true -Dmaven.wagon.http.ssl.ignore.validity.dates=true"

# --- Coding benchmark project: google/gson (real swe_bench_multilingual repo) ---
RUN cd /opt && \
    git clone https://github.com/google/gson.git coding-bench && \
    cd coding-bench && \
    git checkout 7719e73a108c450666613c279bedaed724cd9cc9

# Warm compile: populates ~/.m2 + gson/target/classes so the in-sandbox verify
# can run `mvn -o` (offline). -Djava.version=8 overrides the pom's 1.6.
# `-DskipTests test` (not `compile`) also test-compiles the suite and pulls the
# surefire plugin, so the runtime `mvn -o test` needs no network.
#
# The instance's test patch is baked in as the FAIL_TO_PASS fixture (upstream
# applies it at eval time; our loop keeps it resident so verify can run it).
# It lives under src/test/java, so the find step's `git checkout -- .../main/`
# never reverts it. It is copied BEFORE the warm step so the build proves it
# compiles.
COPY tests/JsonTreeReaderTest.java /opt/coding-bench/gson/src/test/java/com/google/gson/internal/bind/JsonTreeReaderTest.java

# Warm + provider resolution: the second command executes one unrelated, stable
# test class, which pulls surefire's JUnit provider into ~/.m2 (`-DskipTests`
# alone does not necessarily do that).
RUN cd /opt/coding-bench && \
    mvn -B -q -pl gson -DskipTests -Djava.version=8 test && \
    mvn -B -q -pl gson -Djava.version=8 -Dtest=JsonObjectTest -DfailIfNoTests=false test

# Smoke check: compile + run a throwaway probe against the freshly built classes.
RUN printf 'public class _Smoke {\n  public static void main(String[] args) {\n    System.out.println(new com.google.gson.Gson().toJson(new int[] {1, 2, 3}));\n  }\n}\n' > /tmp/_Smoke.java && \
    javac -cp /opt/coding-bench/gson/target/classes /tmp/_Smoke.java && \
    java -cp /opt/coding-bench/gson/target/classes:/tmp _Smoke && \
    rm -f /tmp/_Smoke.java /tmp/_Smoke.class

# Copy the manual testing helper script
COPY bench_helper.sh /opt/coding-bench/bench_helper.sh
RUN chmod +x /opt/coding-bench/bench_helper.sh

# Unset proxy environment variables after build
ENV http_proxy= \
    https_proxy= \
    HTTP_PROXY= \
    HTTPS_PROXY=

# Default command: keep container running for sandbox access
CMD ["sleep", "infinity"]
