import json
import tkinter as tk
from tkinter import ttk, messagebox


class Course:
    def __init__(self, json):
        self.courseName_cn = json['kcmc']
        self.courseName = json['kcmc_en']
        self.courseCode = json['kcdm']
        self.coursePeriod = json['xnxq']
        self.coursePeriod_cn = json['xnxqmc']
        self.courseType = json['kcxzen']
        self.courseType_cn = json['kcxz']
        self.courseType2 = json['kclben']
        self.courseType2_cn = json['kclb']
        self.courseGrade = json['zzcj']
        self.courseDepartment = json['yxmc_en']
        self.courseDepartment_cn = json['yxmc']
        self.credit = json['xf']
        self.grade = json['xscj']
        self.gradeRank = json['pm']
        self.gradeRankField = json['zrs']
        if self.gradeRank is not None and self.gradeRankField is not None:
            self.gradeRankRatio = int(self.gradeRank) / int(self.gradeRankField)
        else:
            self.gradeRankRatio = -1

    def printInfo(self):
        print("Course Name: ", self.courseName)
        print("Course Code: ", self.courseCode)
        print("Course Period: ", self.coursePeriod)
        print("Course Type: ", self.courseType)
        print("Course Type2: ", self.courseType2)
        print("Course Grade: ", self.courseGrade)
        print("Course Department: ", self.courseDepartment)
        print("Credit: ", self.credit)
        print("Grade: ", self.grade)
        print("Grade Rank: ", self.gradeRank)
        print("Grade Rank Field: ", self.gradeRankField)
        print("Grade Rank Ratio: ", self.gradeRankRatio)


class GPACalc:
    def __init__(self):
        self.courses = []

    def add_course(self, course):
        self.courses.append(course)

    def load(self, filename):
        with open(filename, 'r') as file:
            # load from GPA.json
            data = json.load(file)
            for course in data['content']['list']:
                temp = Course(course)
                # temp.printInfo()
                self.add_course(temp)


#     build a GUI frame to select courses and calculate GPA

class GUI:
    def __init__(self, master, courses):
        self.master = master
        self.courses = courses
        self.courses.sort(key=lambda x: x.gradeRankRatio, reverse=True)
        self.selected_courses = []

        self.master.title("GPA Calculator")
        self.master.geometry("1000x800")

        self.course_listbox = tk.Listbox(self.master, selectmode='multiple', width=120, height=30)
        self.course_listbox.pack(pady=20)

        # self.course_listbox.bind('<Button-1>', self.toggle_selection)

        courses_with_ratio = [(course, getattr(course, 'gradeRankRatio', 0)) for course in self.courses]
        courses_with_ratio.sort(key=lambda x: x[1], reverse=True)

        for course, ratio in courses_with_ratio:
            if course.courseGrade is not None and course.credit is not None:
                percentage = f"{ratio * 100:.2f}%"
                course_info = f"{course.courseName if course.courseName is not None else course.courseName_cn} - {course.credit} Credits - Rank Ratio: {percentage}"
                self.course_listbox.insert(tk.END, course_info)

        self.calc_button = ttk.Button(self.master, text="Calculate GPA", command=self.calculate_gpa)
        self.calc_button.pack(pady=10)

        self.gpa_label = ttk.Label(self.master, text="GPA: N/A", font=('Arial', 14))
        self.gpa_label.pack()



    def calculate_gpa(self):
        selections = self.course_listbox.curselection()
        total_points = 0
        total_credits = 0
        for i in selections:
            course = self.courses[i]
            switcher = {
                "A+": 4.00,
                "A": 3.94,
                "A-": 3.85,
                "B+": 3.73,
                "B": 3.55,
                "B-": 3.32,
                "C+": 3.09,
                "C": 2.78,
                "C-": 2.42,
                "D+": 2.08,
                "D": 1.63,
                "D-": 1.15,
                "F": 0
            }
            total_points += float(switcher.get(course.grade)) * float(course.credit)
            total_credits += float(course.credit)
        if total_credits == 0:
            messagebox.showerror("Error", "No courses selected or courses have no credits.")
            return
        gpa = total_points / total_credits
        self.gpa_label.config(text=f"GPA: {gpa:.2f}")


if __name__ == '__main__':
    gpa = GPACalc()
    gpa.load("GPA.json")
    root = tk.Tk()
    app = GUI(root, gpa.courses)
    root.mainloop()
