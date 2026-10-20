document.addEventListener('DOMContentLoaded', function () {
    const chartDataElement = document.getElementById('mindpath-chart-data') || document.getElementById('twin-chart-data');
    if (!chartDataElement) return;

    let chartData;
    try {
        chartData = JSON.parse(chartDataElement.textContent);
    } catch (e) {
        console.error("Failed to parse chart data", e);
        return;
    }

    // Handle clean zero-state for new students without error
    if (chartData.is_new_student) {
        const radarContainer = document.getElementById('radar-chart-container');
        if (radarContainer) {
            radarContainer.innerHTML = `
                <div class="d-flex flex-column align-items-center justify-content-center h-100 text-center text-muted p-3">
                    <i class="bi bi-compass fs-1 text-primary opacity-50 mb-2"></i>
                    <h6 class="fw-semibold text-dark mb-1">Competency Radar Uncalibrated</h6>
                    <p class="small text-muted mb-3" style="max-width: 280px;">Complete the initial baseline assessment to map your strengths across C, Data Structures, and DBMS.</p>
                    <a href="/assessment" class="btn btn-outline-primary btn-sm px-3">Start Initial Assessment</a>
                </div>
            `;
        }
        const barContainer = document.getElementById('bar-chart-container');
        if (barContainer) {
            barContainer.innerHTML = `
                <div class="d-flex flex-column align-items-center justify-content-center h-100 text-center text-muted p-3">
                    <i class="bi bi-bar-chart-line fs-1 text-primary opacity-50 mb-2"></i>
                    <h6 class="fw-semibold text-dark mb-1">Topic Accuracy Awaiting Data</h6>
                    <p class="small text-muted mb-0" style="max-width: 340px;">Real-time accuracy percentages and color-coded mastery tiers will appear immediately after your first quiz submission.</p>
                </div>
            `;
        }
        return;
    }

    // 1. Radar Chart: Subject Competency
    const radarCtx = document.getElementById('subjectRadarChart');
    if (radarCtx && chartData.subjects && chartData.subjects.length > 0) {
        const labels = chartData.subjects.map(s => s.subject);
        const dataValues = chartData.subjects.map(s => s.accuracy);

        new Chart(radarCtx, {
            type: 'radar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Mastery Level (%)',
                    data: dataValues,
                    backgroundColor: 'rgba(79, 70, 229, 0.2)',
                    borderColor: '#4f46e5',
                    borderWidth: 2,
                    pointBackgroundColor: '#4f46e5',
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    r: {
                        angleLines: { color: '#e2e8f0' },
                        grid: { color: '#f1f5f9' },
                        suggestedMin: 0,
                        suggestedMax: 100,
                        ticks: { stepSize: 25, font: { size: 10 } },
                        pointLabels: { font: { size: 12, weight: 'bold' } }
                    }
                },
                plugins: {
                    legend: { display: false }
                }
            }
        });
    }

    // 2. Bar Chart: Topic Accuracy Breakdown
    const barCtx = document.getElementById('topicBarChart');
    if (barCtx && chartData.topics && chartData.topics.length > 0) {
        const topicLabels = chartData.topics.map(t => t.topic_name);
        const topicScores = chartData.topics.map(t => t.accuracy_rate);
        
        // Dynamic colors based on real mastery
        const barColors = chartData.topics.map(t => {
            if (t.total_attempted === 0) return '#94a3b8'; // grey for unassessed
            if (t.accuracy_rate < 50) return '#ef4444';   // red for weak
            if (t.accuracy_rate <= 75) return '#f59e0b';  // amber for average
            return '#10b981';                             // green for strong
        });

        new Chart(barCtx, {
            type: 'bar',
            data: {
                labels: topicLabels,
                datasets: [{
                    label: 'Accuracy (%)',
                    data: topicScores,
                    backgroundColor: barColors,
                    borderRadius: 6,
                    maxBarThickness: 36
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        ticks: { stepSize: 25 },
                        grid: { color: '#f1f5f9' }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { font: { size: 11 } }
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                const topicObj = chartData.topics[context.dataIndex];
                                if (topicObj && topicObj.total_attempted === 0) {
                                    return 'Unassessed (0 attempts)';
                                }
                                return `Accuracy: ${context.parsed.y}% (${topicObj.total_attempted} attempted)`;
                            }
                        }
                    }
                }
            }
        });
    }
});
