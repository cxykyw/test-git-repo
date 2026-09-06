pipeline {
    agent any

    parameters {
        string(name: 'BRANCH', defaultValue: 'main',
               description: '要构建与 AI 审核的分支名（origin 上的分支）')
    }

    options {
        timestamps()
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '10', artifactNumToKeepStr: '5'))
    }

    environment {
        // AI 审核按严重级别阻断打包：blocker/critical 级发现、未审完分片、
        // 超限未审文件使构建失败（AI_REVIEW_BLOCK_SEVERITIES 可调整级别集合）
        DIFY_BLOCKING = '1'
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    // 首次运行参数尚未注册时回落 main；分支名白名单校验防 shell 注入
                    def branch = params.BRANCH ?: 'main'
                    if (!(branch ==~ /^[A-Za-z0-9._\/-]+$/) || branch.contains('..') || branch.startsWith('-')) {
                        error("非法分支名: ${branch}")
                    }
                    echo "构建分支: ${branch}"
                    sh "git checkout -B '${branch}' 'origin/${branch}'"
                    sh "git log -1 --oneline"
                }
            }
        }

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
            sh 'python3 make_report.py || true'
            archiveArtifacts artifacts: '*.zip,report.html', allowEmptyArchive: true, fingerprint: true
        }
    }
}
