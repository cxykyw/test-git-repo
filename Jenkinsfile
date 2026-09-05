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
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; mkdir -p scan; semgrep scan --config p/default --config p/security-audit --config p/owasp-top-ten --json --metrics=off --quiet -o scan/semgrep.json || true'
                    }
                }
                stage('Gitleaks') {
                    steps {
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; mkdir -p scan; gitleaks detect --no-git --report-format json --report-path scan/gitleaks.json || true'
                    }
                }
                stage('Trivy') {
                    steps {
                        sh 'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; mkdir -p scan; trivy fs --scanners vuln --format json --output scan/trivy.json . || true'
                    }
                }
            }
        }

        stage('Quality Gate') {
            steps {
                sh './gate.sh'
            }
        }

        stage('AI Review') {
            steps {
                sh 'python3 ai_review.py'
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
            archiveArtifacts artifacts: '*.zip,report.json', allowEmptyArchive: true, fingerprint: true
        }
    }
}
