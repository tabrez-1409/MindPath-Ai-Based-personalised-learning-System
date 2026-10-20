import sqlite3
import os
import shutil
import time
from datetime import datetime

def get_db_path():
    """Returns the database path, allowing override via environment variable for test isolation."""
    return os.environ.get('LEARNTWIN_DB_PATH', os.path.join(os.path.dirname(__file__), 'learntwin.db'))

def get_connection(db_path=None, timeout=30.0):
    """
    Returns a SQLite connection configured with:
    - 30s connection timeout & PRAGMA busy_timeout to resolve lock contention
    - foreign keys enabled
    - Row factory for named column access
    """
    target_path = db_path or get_db_path()
    conn = sqlite3.connect(target_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def enable_wal_mode(db_path=None):
    """Enables Write-Ahead Logging (WAL) mode for high concurrency."""
    target_path = db_path or get_db_path()
    if not os.path.exists(target_path):
        return
    conn = get_connection(target_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    finally:
        conn.close()

def init_db(db_path=None):
    target_path = db_path or get_db_path()
    conn = get_connection(target_path)
    try:
        cursor = conn.cursor()

        # 1. Users table (real accounts with hashed passwords)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            course TEXT,
            academic_year TEXT,
            learning_goal TEXT,
            role TEXT DEFAULT 'student',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # 2. Topics table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT
        )
        ''')
        cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_subject_name 
        ON topics(LOWER(subject), LOWER(name))
        ''')

        # 3. Questions table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_option TEXT NOT NULL,
            difficulty TEXT NOT NULL CHECK(difficulty IN ('easy', 'medium', 'hard')),
            explanation TEXT,
            FOREIGN KEY (topic_id) REFERENCES topics (id)
        )
        ''')

        # 4. Quiz Attempts (Supports topic adaptive quizzes & general assessments)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic_id INTEGER,
            quiz_type TEXT DEFAULT 'adaptive',
            score INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            time_taken_seconds INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (topic_id) REFERENCES topics (id)
        )
        ''')

        # 5. Quiz Answers (Detailed question-level responses for the twin)
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS quiz_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_option TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            time_spent_seconds INTEGER NOT NULL,
            FOREIGN KEY (attempt_id) REFERENCES quiz_attempts (id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES questions (id)
        )
        ''')

        # 6. Student Digital Twin Profile
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS student_twin_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            total_attempted INTEGER DEFAULT 0,
            total_correct INTEGER DEFAULT 0,
            accuracy_rate REAL DEFAULT 0.0,
            avg_time_per_question REAL DEFAULT 0.0,
            mastery_status TEXT DEFAULT 'AVERAGE' CHECK(mastery_status IN ('WEAK', 'AVERAGE', 'STRONG')),
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, topic_id),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics (id)
        )
        ''')

        conn.commit()
    finally:
        conn.close()

    # Enable WAL mode after tables are ensured
    enable_wal_mode(target_path)

def migrate_db(db_path=None):
    """
    Performs safe, non-destructive migration of existing databases:
    - Adds missing columns in users (password_hash, course, academic_year, learning_goal, role)
    - Adds quiz_type column to quiz_attempts if missing
    - Cleans out legacy demo records (Aarav/Sneha) and associated fake records
    - Ensures WAL mode
    """
    target_path = db_path or get_db_path()
    if not os.path.exists(target_path):
        init_db(target_path)
        seed_educational_content(target_path)
        return

    enable_wal_mode(target_path)
    conn = get_connection(target_path)
    try:
        cursor = conn.cursor()

        # Check columns in users
        cursor.execute("PRAGMA table_info(users)")
        user_cols = [col[1] for col in cursor.fetchall()]

        if user_cols:
            if 'password_hash' not in user_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")
            if 'course' not in user_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN course TEXT DEFAULT ''")
            if 'academic_year' not in user_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN academic_year TEXT DEFAULT ''")
            if 'learning_goal' not in user_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN learning_goal TEXT DEFAULT ''")
            if 'role' not in user_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'")

        # Check quiz_attempts
        cursor.execute("PRAGMA table_info(quiz_attempts)")
        attempt_info = cursor.fetchall()
        attempt_cols = [col[1] for col in attempt_info]
        if attempt_cols and 'quiz_type' not in attempt_cols:
            cursor.execute("ALTER TABLE quiz_attempts ADD COLUMN quiz_type TEXT DEFAULT 'adaptive'")

        topic_col_info = [c for c in attempt_info if c[1] == 'topic_id']
        if topic_col_info and topic_col_info[0][3] == 1:  # if notnull is 1
            cursor.execute("PRAGMA foreign_keys = OFF")
            cursor.execute('''
            CREATE TABLE quiz_attempts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                topic_id INTEGER,
                quiz_type TEXT DEFAULT 'adaptive',
                score INTEGER NOT NULL,
                total_questions INTEGER NOT NULL,
                time_taken_seconds INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (topic_id) REFERENCES topics (id)
            )
            ''')
            cursor.execute('INSERT INTO quiz_attempts_new (id, user_id, topic_id, quiz_type, score, total_questions, time_taken_seconds, created_at) SELECT id, user_id, topic_id, quiz_type, score, total_questions, time_taken_seconds, created_at FROM quiz_attempts')
            cursor.execute('DROP TABLE quiz_attempts')
            cursor.execute('ALTER TABLE quiz_attempts_new RENAME TO quiz_attempts')
            cursor.execute("PRAGMA foreign_keys = ON")

        # Clean legacy hardcoded demo accounts so real users don't see fake records
        cursor.execute("SELECT id FROM users WHERE email IN ('aarav@learntwin.edu', 'sneha@learntwin.edu')")
        demo_user_ids = [row[0] for row in cursor.fetchall()]

        if demo_user_ids:
            placeholders = ','.join('?' for _ in demo_user_ids)
            # Delete demo answers
            cursor.execute(f'''
                DELETE FROM quiz_answers WHERE attempt_id IN (
                    SELECT id FROM quiz_attempts WHERE user_id IN ({placeholders})
                )
            ''', demo_user_ids)
            # Delete demo attempts
            cursor.execute(f"DELETE FROM quiz_attempts WHERE user_id IN ({placeholders})", demo_user_ids)
            # Delete demo profiles
            cursor.execute(f"DELETE FROM student_twin_profile WHERE user_id IN ({placeholders})", demo_user_ids)
            # Delete demo users
            cursor.execute(f"DELETE FROM users WHERE id IN ({placeholders})", demo_user_ids)

        # Ensure unique index on topic subject and name
        cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_subject_name 
        ON topics(LOWER(subject), LOWER(name))
        ''')

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def seed_educational_content(db_path=None):
    """
    Populates shared educational curriculum (Topics and Questions).
    Strictly avoids creating any user accounts or student performance records.
    """
    target_path = db_path or get_db_path()
    conn = get_connection(target_path)
    try:
        cursor = conn.cursor()

        # Seed Topics if empty
        cursor.execute("SELECT COUNT(*) FROM topics")
        if cursor.fetchone()[0] == 0:
            topics_data = [
                ("C Programming", "Pointers & Memory", "Addresses, dereferencing, dynamic allocation"),
                ("C Programming", "Recursion & Functions", "Call stack, base condition, recursive problem solving"),
                ("Data Structures", "Linked Lists", "Singly, doubly, circular node chaining"),
                ("Data Structures", "Stacks & Queues", "LIFO, FIFO, push, pop, enqueue, dequeue"),
                ("DBMS", "Relational Model & Keys", "Primary key, foreign key, candidate keys"),
                ("DBMS", "Normalization", "1NF, 2NF, 3NF, BCNF, removing data redundancy")
            ]
            cursor.executemany("INSERT INTO topics (subject, name, description) VALUES (?, ?, ?)", topics_data)

        # Seed Questions if empty
        cursor.execute("SELECT COUNT(*) FROM questions")
        if cursor.fetchone()[0] == 0:
            questions_data = [
                # Topic 1: Pointers & Memory (C Programming)
                (1, "What does the unary operator '&' return when applied to a variable in C?", 
                 "The value of the variable", "The memory address of the variable", "The data type size", "The pointer reference count",
                 "B", "easy", "The '&' operator is the address-of operator; it yields the memory location of its operand."),

                (1, "Which function in stdlib.h allocates memory and initializes all bytes to zero?", 
                 "malloc()", "realloc()", "calloc()", "alloc()",
                 "C", "medium", "calloc(n, size) allocates memory for an array of elements and zeroes out every byte, unlike malloc()."),

                (1, "What causes a dangling pointer in C?", 
                 "A pointer pointing to a freed or out-of-scope memory location", "A pointer initialized to NULL", "A pointer exceeding integer limit", "A pointer pointing to constant memory",
                 "A", "hard", "A dangling pointer arises when memory is deallocated via free(), but the pointer variable is not reset to NULL."),

                # Topic 2: Recursion & Functions (C Programming)
                (2, "What happens if a recursive function does not have a valid base case?", 
                 "It returns 0 immediately", "It leads to stack overflow", "Compilation error occurs", "It executes only once",
                 "B", "easy", "Without a base case, recursive calls continue infinitely until the call stack memory is exhausted, throwing stack overflow."),

                (2, "What is the time complexity of a standard naive recursive Fibonacci function?", 
                 "O(n)", "O(n log n)", "O(2^n)", "O(n^2)",
                 "C", "medium", "Each call spawns two subsequent recursive calls (T(n) = T(n-1) + T(n-2)), resulting in exponential O(2^n) complexity."),

                (2, "In tail recursion, where does the recursive call happen?", 
                 "As the very first statement", "Inside a loop only", "As the final statement in the function before returning", "Inside an exception handler",
                 "C", "hard", "Tail recursion happens when the recursive call is the last executed instruction, allowing compilers to optimize stack frames."),

                # Topic 3: Linked Lists (Data Structures)
                (3, "What is the time complexity to insert a node at the head of a singly linked list?", 
                 "O(1)", "O(n)", "O(log n)", "O(n^2)",
                 "A", "easy", "Inserting at head only requires updating the new node's next pointer and head pointer, taking constant O(1) time."),

                (3, "Which linked list type allows bidirectional traversal?", 
                 "Singly Linked List", "Doubly Linked List", "Circular Singly Linked List", "Skip List only",
                 "B", "medium", "A doubly linked list contains both 'next' and 'prev' pointers per node, enabling forward and backward traversal."),

                (3, "What is the primary condition to detect a loop in a linked list using Floyd's Cycle Algorithm?", 
                 "Fast and slow pointers become NULL", "Fast pointer equals slow pointer", "Fast pointer reaches twice the head value", "Slow pointer turns negative",
                 "B", "hard", "Floyd's algorithm uses two pointers moving at speeds 1 and 2. If a cycle exists, the fast pointer will eventually catch up and meet the slow pointer."),

                # Topic 4: Stacks & Queues (Data Structures)
                (4, "Which principle governs the operation of a Stack data structure?", 
                 "FIFO (First In First Out)", "LIFO (Last In First Out)", "Priority First", "Random Access",
                 "B", "easy", "A stack operates on Last-In-First-Out: the most recently pushed element is the first one popped."),

                (4, "Which data structure is most suitable for evaluating postfix mathematical expressions?", 
                 "Queue", "Stack", "Binary Tree", "Hash Map",
                 "B", "medium", "A stack holds operands until an operator arrives, popping the top two operands to compute the intermediate result."),

                (4, "How can a Queue be implemented efficiently using two Stacks?", 
                 "One stack for enqueue, one stack for dequeue transfers", "Push both stacks at once", "Reverse the stack pointers directly", "Circular indexing on both stacks",
                 "A", "hard", "Stack 1 receives incoming elements (enqueue). For dequeue, if Stack 2 is empty, all elements from Stack 1 are popped and pushed into Stack 2, reversing order."),

                # Topic 5: Relational Model & Keys (DBMS)
                (5, "What key uniquely identifies each record in a relational database table?", 
                 "Foreign Key", "Secondary Key", "Primary Key", "Composite Index",
                 "C", "easy", "A primary key is a column or set of columns that uniquely identifies every tuple in a table and cannot contain NULL."),

                (5, "What is a Foreign Key used for in a relational database?", 
                 "To encrypt table data", "To enforce referential integrity between two tables", "To increase query sorting speed only", "To permit duplicate keys",
                 "B", "medium", "A foreign key matches the primary key of another table to maintain relational integrity between related tables."),

                (5, "Can a table possess multiple candidate keys? If so, how is the primary key selected?", 
                 "No, only one candidate key exists per table", "Yes, any candidate key can be designated as the primary key", "Yes, all candidate keys must be merged into one", "No, database engines forbid multiple candidates",
                 "B", "hard", "A table can have multiple minimal superkeys (candidate keys); the database designer picks one to serve as the primary key, leaving the others as alternate keys."),

                # Topic 6: Normalization (DBMS)
                (6, "What is the primary objective of Normalization in DBMS?", 
                 "To increase table storage size", "To reduce data redundancy and eliminate anomalies", "To remove all primary keys", "To combine all data into one table",
                 "B", "easy", "Normalization structures schemas to minimize insertion, deletion, and update anomalies and avoid duplicated data."),

                (6, "A relation is in 2NF if it is in 1NF and contains no:", 
                 "Transitive dependency", "Partial dependency on candidate key", "Multivalued dependency", "Composite primary key",
                 "B", "medium", "2NF forbids non-prime attributes from depending on a proper subset of any candidate key (no partial dependency)."),

                (6, "Which normal form requires that every determinant in a functional dependency X -> Y must be a super key?", 
                 "1NF", "2NF", "3NF", "Boyce-Codd Normal Form (BCNF)",
                 "D", "hard", "BCNF is a stricter version of 3NF. For every non-trivial functional dependency X -> Y, X must strictly be a superkey.")
            ]

            cursor.executemany('''
            INSERT INTO questions 
            (topic_id, question_text, option_a, option_b, option_c, option_d, correct_option, difficulty, explanation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', questions_data)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# User Account Helpers with Reliable Lifecycle
def get_user_by_email(email, db_path=None):
    if not email:
        return None
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_user_by_id(user_id, db_path=None):
    if not user_id:
        return None
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, email, course, academic_year, learning_goal, role, created_at FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def create_user(name, email, password_hash, course=None, academic_year=None, learning_goal=None, role='student', db_path=None):
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO users (name, email, password_hash, course, academic_year, learning_goal, role)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name.strip(), email.strip().lower(), password_hash, course or '', academic_year or '', learning_goal or '', role))
        user_id = cursor.lastrowid
        conn.commit()
        return user_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# User-Defined Custom Topics and AI Question Helpers
def get_or_create_topic(subject, name, description=None, db_path=None):
    """
    Finds an existing topic by case-insensitive subject and topic name,
    or inserts a new one if it does not exist.
    Returns the topic dictionary: {'id': ..., 'subject': ..., 'name': ..., 'description': ...}
    """
    clean_subj = subject.strip()
    clean_name = name.strip()
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, subject, name, description 
            FROM topics 
            WHERE LOWER(TRIM(subject)) = LOWER(?) AND LOWER(TRIM(name)) = LOWER(?)
        """, (clean_subj, clean_name))
        row = cursor.fetchone()
        if row:
            return dict(row)

        desc = (description or f"User-created topic: {clean_name} under {clean_subj}").strip()
        cursor.execute("""
            INSERT INTO topics (subject, name, description)
            VALUES (?, ?, ?)
        """, (clean_subj, clean_name, desc))
        new_id = cursor.lastrowid
        conn.commit()
        return {
            'id': new_id,
            'subject': clean_subj,
            'name': clean_name,
            'description': desc
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_all_subjects_and_topics(db_path=None):
    """
    Returns unique subjects and all topics grouped/listed for suggestions in UI datalists.
    Returns a dict: {'subjects': [str, ...], 'topics': [{'id': int, 'subject': str, 'name': str, 'description': str}, ...]}
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT subject FROM topics ORDER BY subject COLLATE NOCASE ASC")
        subjects = [row[0] for row in cursor.fetchall() if row[0]]

        cursor.execute("SELECT id, subject, name, description FROM topics ORDER BY subject COLLATE NOCASE ASC, name COLLATE NOCASE ASC")
        topics = [dict(row) for row in cursor.fetchall()]
        return {
            'subjects': subjects,
            'topics': topics
        }
    finally:
        conn.close()

def save_generated_questions(topic_id, questions_list, db_path=None):
    """
    Saves an array of validated question dictionaries into the database under topic_id.
    Each item must have: question_text, option_a, option_b, option_c, option_d,
    correct_option, difficulty, explanation.
    Returns the count of successfully inserted questions.
    """
    if not questions_list:
        return 0
    conn = get_connection(db_path)
    try:
        cursor = conn.cursor()
        inserted = 0
        for q in questions_list:
            cursor.execute("""
                INSERT INTO questions (topic_id, question_text, option_a, option_b, option_c, option_d, correct_option, difficulty, explanation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                topic_id,
                q['question_text'].strip(),
                q['option_a'].strip(),
                q['option_b'].strip(),
                q['option_c'].strip(),
                q['option_d'].strip(),
                q['correct_option'].strip().upper(),
                q['difficulty'].strip().lower(),
                (q.get('explanation') or '').strip()
            ))
            inserted += 1
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def seed_data(db_path=None):
    """Aliases seed_educational_content without creating demo users."""
    seed_educational_content(db_path)

if __name__ == '__main__':
    init_db()
    migrate_db()
    seed_educational_content()
    print("Database initialized, migrated, and WAL mode enabled successfully.")
