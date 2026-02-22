"""
Module Architecture Analysis & Visualization Generator

This script:
1. Analyzes the codebase to extract module dependencies
2. Generates a detailed dependency matrix
3. Creates visual diagrams (PNG/SVG) of module connections
4. Outputs metrics about module coupling and complexity

Usage:
    python generate_diagrams.py [--format png|svg|all] [--output ./images/]
"""

import os
import re
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, Set, List, Tuple

# Try to import graphing libraries
try:
    import graphviz
    HAS_GRAPHVIZ = True
except ImportError:
    HAS_GRAPHVIZ = False
    print("⚠ graphviz not installed. Install with: pip install graphviz")

# Skip matplotlib due to numpy 2.x compatibility issues
HAS_MATPLOTLIB = False


class ModuleAnalyzer:
    """Analyze Python module dependencies and generate reports."""
    
    def __init__(self, root_path: str = "d:\\automation-project"):
        self.root = Path(root_path)
        self.src_path = self.root / "src"
        self.modules = {}  # {module_name: {info}}
        self.import_graph = defaultdict(set)  # {from_module: {to_modules}}
        self.call_graph = defaultdict(set)    # {caller: {callees}}
        
    def analyze(self):
        """Scan all Python files and extract dependency information."""
        print(f"[*] Analyzing modules in {self.src_path}...")
        
        if not self.src_path.exists():
            print(f"[!] Path not found: {self.src_path}")
            return
        
        # Find all Python files
        py_files = list(self.src_path.glob("*.py"))
        
        for py_file in py_files:
            module_name = py_file.stem
            if module_name.startswith("__"):
                continue
                
            self._analyze_file(module_name, py_file)
        
        # Also analyze main.py
        main_py = self.root / "main.py"
        if main_py.exists():
            self._analyze_file("main", main_py)
    
    def _analyze_file(self, module_name: str, filepath: Path):
        """Extract imports and basic stats from a Python file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                lines = content.split('\n')
                
                # Count lines
                code_lines = len([l for l in lines if l.strip() and not l.strip().startswith('#')])
                
                # Extract imports
                imports = self._extract_imports(content, module_name)
                
                # Extract functions/classes
                functions = list(self._extract_functions(content))
                classes = list(self._extract_classes(content))
                
                self.modules[module_name] = {
                    'path': str(filepath),
                    'lines': len(lines),
                    'code_lines': code_lines,
                    'imports': imports,  # Keep as set for now
                    'functions': functions,
                    'classes': classes,
                }
        except Exception as e:
            print(f"[!] Error analyzing {module_name}: {e}")
    
    def _extract_imports(self, content: str, current_module: str) -> Set[str]:
        """Extract imports from Python code."""
        imports = set()
        
        # Match: from src.module import ...
        for match in re.finditer(r'^from\s+src\.(\w+)\s+import', content, re.MULTILINE):
            imports.add(match.group(1))
            self.import_graph[current_module].add(match.group(1))
        
        # Match: import src.module
        for match in re.finditer(r'^import\s+src\.(\w+)', content, re.MULTILINE):
            imports.add(match.group(1))
            self.import_graph[current_module].add(match.group(1))
        
        return imports
    
    def _extract_functions(self, content: str) -> Set[str]:
        """Extract function definitions from Python code."""
        functions = set()
        for match in re.finditer(r'^def\s+(\w+)\s*\(', content, re.MULTILINE):
            func_name = match.group(1)
            if not func_name.startswith('_'):  # Skip private
                functions.add(func_name)
        return functions
    
    def _extract_classes(self, content: str) -> Set[str]:
        """Extract class definitions from Python code."""
        classes = set()
        for match in re.finditer(r'^class\s+(\w+)', content, re.MULTILINE):
            classes.add(match.group(1))
        return classes
    
    def get_dependency_degree(self) -> Dict[str, int]:
        """Calculate how many modules depend on each module."""
        degree = defaultdict(int)
        for dependent, dependencies in self.import_graph.items():
            for dep in dependencies:
                degree[dep] += 1
        return dict(degree)
    
    def generate_json_report(self, output_path: str = "module_architecture.json") -> str:
        """Generate a JSON report of module structure."""
        report = {
            'modules': {
                name: {
                    **info,
                    'imports': list(info['imports']),  # Convert set to list
                    'functions': info['functions'],     # Already a list
                    'classes': info['classes'],         # Already a list
                }
                for name, info in self.modules.items()
            },
            'import_graph': {k: list(v) for k, v in self.import_graph.items()},
            'dependency_degree': self.get_dependency_degree(),
            'total_modules': len(self.modules),
            'total_imports': sum(len(v) for v in self.import_graph.values()),
        }
        
        output_file = self.root / output_path
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        print(f"[+] JSON report written to {output_file}")
        return str(output_file)
    
    def generate_text_report(self) -> str:
        """Generate a detailed text report."""
        report = []
        report.append("=" * 80)
        report.append("MODULE ARCHITECTURE — DETAILED BREAKDOWN")
        report.append("=" * 80)
        report.append("")
        
        # Module inventory
        report.append("MODULE INVENTORY")
        report.append("-" * 80)
        for module_name in sorted(self.modules.keys()):
            info = self.modules[module_name]
            report.append(f"\n{module_name.upper()}")
            report.append(f"  File: {info['path']}")
            report.append(f"  Lines: {info['lines']} total ({info['code_lines']} code)")
            report.append(f"  Classes: {len(info['classes'])}")
            report.append(f"  Functions: {len(info['functions'])}")
            if info['imports']:
                report.append(f"  Imports: {', '.join(sorted(info['imports']))}")
            
            if info['classes']:
                report.append(f"    Classes: {', '.join(sorted(info['classes']))}")
            if info['functions']:
                report.append(f"    Functions: {', '.join(sorted(info['functions'][:3]))}")
                if len(info['functions']) > 3:
                    report.append(f"      ... and {len(info['functions']) - 3} more")
        
        # Dependency matrix
        report.append("\n\n" + "=" * 80)
        report.append("DEPENDENCY MATRIX")
        report.append("=" * 80)
        degree = self.get_dependency_degree()
        
        report.append(f"\n{'Module':<20} | {'Imports':<20} | {'Imported By':<15}")
        report.append("-" * 80)
        
        for module_name in sorted(self.modules.keys()):
            imports = ", ".join(sorted(self.import_graph.get(module_name, set()))) or "—"
            imported_by = degree.get(module_name, 0)
            report.append(f"{module_name:<20} | {imports:<20} | {imported_by}")
        
        # Connection summary
        report.append("\n\n" + "=" * 80)
        report.append("CONNECTION SUMMARY")
        report.append("=" * 80)
        total_imports = sum(len(v) for v in self.import_graph.values())
        report.append(f"\nTotal Direct Imports: {total_imports}")
        report.append(f"Most Central Module: {max(degree, key=degree.get) if degree else 'N/A'} (imported by {max(degree.values()) if degree else 0})")
        
        return "\n".join(report)
    
    def generate_graphviz_diagram(self, output_format: str = "png", output_path: str = "images/") -> str:
        """Generate a diagram using Graphviz."""
        if not HAS_GRAPHVIZ:
            print("[!] Graphviz not installed")
            return ""
        
        output_dir = Path(output_path)
        output_dir.mkdir(exist_ok=True)
        
        # Create directed graph
        dot = graphviz.Digraph(
            name='module_architecture',
            comment='Module Architecture Diagram',
            engine='dot'
        )
        
        dot.attr(rankdir='TB', size='12,8', ratio='fill')
        dot.attr('node', shape='box', style='filled', fillcolor='lightblue', fontname='Arial')
        dot.attr('edge', fontname='Arial', fontsize='10')
        
        # Add nodes
        colors = {
            'main': '#4a90e2',
            'utils': '#50c878',
            'coordinator': '#f5a623',
            'auth': '#7ed321',
            'navigator': '#7ed321',
            'batch': '#bd10e0',
            'dataset': '#bd10e0',
        }
        
        for module_name in sorted(self.modules.keys()):
            color = 'white'
            for key, col in colors.items():
                if key in module_name:
                    color = col
                    break
            
            info = self.modules[module_name]
            label = f"{module_name}\\n({info['code_lines']} lines)"
            dot.node(module_name, label, fillcolor=color, fontcolor='white' if color != 'white' else 'black')
        
        # Add edges
        for source, targets in sorted(self.import_graph.items()):
            for target in sorted(targets):
                dot.edge(source, target, label='imports', color='#333333')
        
        # Generate file
        output_file = output_dir / f'module_architecture.{output_format}'
        dot.render(str(output_file.with_suffix('')), format=output_format, cleanup=True)
        
        print(f"[+] Graphviz diagram generated: {output_file}")
        return str(output_file)
    
    def generate_matplotlib_diagram(self, output_path: str = "images/") -> str:
        """Generate a network diagram using matplotlib."""
        if not HAS_MATPLOTLIB:
            print("[!] Matplotlib/NetworkX not installed")
            return ""
        
        output_dir = Path(output_path)
        output_dir.mkdir(exist_ok=True)
        
        # Build network graph
        G = nx.DiGraph()
        degree = self.get_dependency_degree()
        
        for module_name in self.modules.keys():
            G.add_node(module_name, 
                      size=100 + degree.get(module_name, 0) * 200,
                      code_lines=self.modules[module_name]['code_lines'])
        
        for source, targets in self.import_graph.items():
            for target in targets:
                G.add_edge(source, target)
        
        # Draw
        plt.figure(figsize=(14, 10))
        pos = nx.spring_layout(G, k=2, iterations=50, seed=42)
        
        # Node colors based on type
        node_colors = []
        node_sizes = []
        for node in G.nodes():
            if 'main' in node:
                node_colors.append('#4a90e2')
            elif 'utils' in node:
                node_colors.append('#50c878')
            elif 'coordinator' in node:
                node_colors.append('#f5a623')
            elif 'auth' in node or 'navigator' in node:
                node_colors.append('#7ed321')
            elif 'batch' in node or 'dataset' in node:
                node_colors.append('#bd10e0')
            else:
                node_colors.append('#cccccc')
            
            node_sizes.append(500 + degree.get(node, 0) * 300)
        
        # Draw the network
        nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes, 
                              alpha=0.8, edgecolors='black', linewidths=2)
        nx.draw_networkx_labels(G, pos, font_size=9, font_weight='bold')
        nx.draw_networkx_edges(G, pos, edge_color='#666666', arrows=True, 
                              arrowsize=20, arrowstyle='->', width=2, 
                              connectionstyle='arc3,rad=0.1', alpha=0.6)
        
        plt.title('Roboflow Automation — Module Dependency Graph', fontsize=16, fontweight='bold')
        plt.tight_layout()
        
        output_file = output_dir / 'module_dependencies_networkx.png'
        plt.savefig(str(output_file), dpi=300, bbox_inches='tight')
        
        print(f"[+] Matplotlib diagram generated: {output_file}")
        return str(output_file)


def main():
    """Main entry point."""
    analyzer = ModuleAnalyzer()
    analyzer.analyze()
    
    # Generate reports
    print("\n[*] Generating reports...")
    analyzer.generate_json_report()
    
    text_report = analyzer.generate_text_report()
    print("\n" + text_report)
    
    # Save text report
    report_file = Path("MODULE_BREAKDOWN.txt")
    with open(report_file, 'w') as f:
        f.write(text_report)
    print(f"\n[+] Text report saved to {report_file}")
    
    # Generate visual diagrams
    print("\n[*] Generating visual diagrams...")
    if HAS_GRAPHVIZ:
        analyzer.generate_graphviz_diagram()
    if HAS_MATPLOTLIB:
        analyzer.generate_matplotlib_diagram()
    
    print("\n[+] ✓ Architecture analysis complete!")


if __name__ == "__main__":
    main()
