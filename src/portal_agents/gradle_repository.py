"""Expose dependency files staged inside a single offline workspace to Gradle."""

GRADLE_REPOSITORIES_DIR = "/tmp/portal-gradle-repositories"

# Enable these repositories only when the agent has fetched files. Leave the project's
# original repositories and the image's Gradle dependency cache available.
GRADLE_REPOSITORY_INIT = """
def root = new File('/tmp/portal-gradle-repositories')
def repos = root.listFiles()?.findAll { it.directory && it.canonicalFile == it }?.sort() ?: []
def addStaged = { handler ->
    repos.each { dir ->
        handler.maven {
            url = dir.toURI()
            metadataSources {
                gradleMetadata()
                mavenPom()
                artifact()
            }
        }
    }
}
gradle.settingsEvaluated { settings ->
    def central = settings.hasProperty('dependencyResolutionManagement') ?
        settings.dependencyResolutionManagement : null
    def hasCentralRepos = central != null && !central.repositories.empty
    if (central != null) {
        addStaged(central.repositories)
    }
    gradle.beforeProject { project ->
        addStaged(project.buildscript.repositories)
        if (central == null || central.repositoriesMode.get().name() == 'PREFER_PROJECT') {
            if (!hasCentralRepos || !project.repositories.empty) {
                addStaged(project.repositories)
            } else {
                // Empty project repositories inherit the settings repositories in this mode.
                project.afterEvaluate {
                    if (!project.repositories.empty) addStaged(project.repositories)
                }
            }
        }
    }
}
""".strip()
