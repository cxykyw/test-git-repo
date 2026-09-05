pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
    }

    stages {
        stage('Security Scan') {
            parallel {
                stage('Semgrep SAST') {
                    steps {
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; semgrep scan --config p/default --json --metrics=off --quiet -o semgrep.json || true'
                    }
                }
                stage('Gitleaks') {
                    steps {
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; gitleaks detect --no-git --report-format json --report-path gitleaks.json || true'
                    }
                }
                stage('Trivy') {
                    steps {
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; trivy fs --scanners vuln --format json --output trivy.json . || true'
                    }
                }
            }
        }

        stage('Quality Gate') {
            steps {
                sh './gate.sh'
            }
        }

        stage('Package') {
            steps {
                sh './build.sh'
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: '*.json,*.zip', allowEmptyArchive: true, fingerprint: true
        }
    }
}
